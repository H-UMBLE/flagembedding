import os
import json
import logging
import datasets
from typing import List
from accelerate import Accelerator
from torch.utils.data import DataLoader
from transformers import HfArgumentParser
from dataclasses import dataclass, field, asdict

from src.lm import (
    LM, 
    LMArgs,
    GenerationArgs
)
from src.retrieval import (
    RetrievalArgs, 
    RetrievalMetric,
)
from src.utils.util import makedirs, remove_eos, DefaultDataCollator, DatasetProcessFn, FileLogger
from .eval_retrieval import main as retrieval_main

logger = logging.getLogger(__name__)


PROPID_2_TEMPLATE = {
    22: "What is {}'s occupation?",
    218: "In what city was {} born?",
    91: "What genre is {}?",
    257: "Who is the father of {}?",
    182: "In what country is {}?",
    164: "Who was the producer of {}?",
    526: "Who was the director of {}?",
    97: "What is {} the capital of?",
    533: "Who was the screenwriter for {}?",
    639: "Who was the composer of {}?",
    472: "What color is {}?",
    106: "What is the religion of {}?",
    560: "What sport does {} play?",
    484: "Who is the author of {}?",
    292: "Who is the mother of {}?",
    422: "What is the capital of {}?"
}


@dataclass
class PopQAArgs(LMArgs, RetrievalArgs):
    output_dir: str = field(
        default="data/results/popqa",
    )
    eval_data: str = field(
        default="llm-embedder:qa/popqa/test.json",
        metadata={'help': 'Path to the test file.'}
    )

    few_shot: int = field(
        default=15,
        metadata={'help': 'How many few shot train samples?'},
    )

    hits: int = field(
        default=10,
        metadata={'help': 'How many hits per query?'},
    )
    key_num: int = field(
        default=3,
        metadata={'help': 'How many docs to provide in prompt?'},
    )
    corpus: str = field(
        default="llm-embedder:qa/nq/corpus.json",
        metadata={'help': 'Corpus path for retrieval.'}
    )
    key_template: str = field(
        default="{title} {text}",
        metadata={'help': 'How to concatenate columns in the corpus to form one key?'}
    )
    key_max_length: int = field(
        default=128,
        metadata={'help': 'How many tokens at maximum in a key.'}
    )
    metrics: List[str] = field(
        default_factory=lambda: ["collate_key"],
    )
    save_to_output: bool = field(
        default=True,
        metadata={'help': 'Save the result/key/negative to output_dir? If not true, they will be saved next to the eval_data.'}
    )

    log_path: str = field(
        default="data/results/popqa/popqa.log",
        metadata={'help': 'Path to the file for logging.'}
    )


@dataclass
class GenerationArgs(GenerationArgs):
    max_new_tokens: int = field(
        default=16,
        metadata={'help': 'Maximum new tokens to generate.'}
    )
    eos_token_id: int = 13


def process_popqa(tokenizer, context_max_length=2048, key_num=3, few_shot=0, train_data=None, cache_dir=None, is_encoder_decoder=False):
    test = tokenizer("test", return_special_tokens_mask=True)["special_tokens_mask"]
    has_bos = has_eos = False
    if test[0] == 1:
        has_bos = True
    if test[-1] == 1:
        has_eos = True

    if few_shot > 0:
        assert train_data is not None
        assert few_shot // (len(PROPID_2_TEMPLATE) - 1), f"Make sure the number of few shot examples is a multiple of the template number!"
        train_dataset = datasets.load_dataset("json", data_files=train_data, cache_dir=cache_dir, split="train")
        train_df = train_dataset.to_pandas()
        train_df = {k: v[:few_shot] for k, v in train_df.groupby("prop_id")}
        nshot_per_template = few_shot // (len(PROPID_2_TEMPLATE) - 1)

    def _prepare_sample(query, obj=None, **kwds):
        sample = f"Q: {query} A:"
        if obj is not None:
            sample = sample + " " + obj
        return sample

    def _prepare_retrieval(keys):
        if keys is not None:
            keys = keys[:key_num]
            keys = "\n".join(keys)
            keys = f"Knowledge: {keys}"
        else:
            keys = ""
        return keys

    @DatasetProcessFn()
    def _process(query, query_id, prop_id, key=None, _index=None, **kwds):
        """Yield keys and query with a prompt template"""
        output = {}
        query = query.strip()

        knowledge = _prepare_retrieval(key)

        train_samples_max_length = context_max_length - len(tokenizer.encode("\n\n" if len(knowledge) else "" + _prepare_sample(query), add_special_tokens=False)) - int(has_bos)

        if few_shot > 0:
            train_samples = ""
            train_samples_length = 0
            
            for k, df in train_df.items():
                # avoid contamination
                if k == prop_id:
                    continue
                for sample in df.sample(nshot_per_template).iloc:
                    train_sample = _prepare_sample(**sample) + "\n\n"
                    # make sure the length of training samples does not exceed maximum length
                    if train_samples_length + len(tokenizer.encode(train_sample)) > train_samples_max_length:
                        break
                    else:                    
                        train_samples += train_sample
                        train_samples_length += len(tokenizer.encode(train_sample))
        else:
            train_samples = ""

        left = knowledge
        # \n\n to split retrieved knowledge
        right = "\n\n" + train_samples + _prepare_sample(query)
        
        pair = tokenizer.encode(left, right, add_special_tokens=False, truncation="only_first", max_length=context_max_length - int(has_bos) - int(has_eos))

        # strip spaces and \n in the head (when there is no retrieved passage)
        seq = tokenizer.decode(pair).strip()
        inputs = tokenizer(seq, return_token_type_ids=False)

        if has_eos and not is_encoder_decoder:
            inputs = remove_eos(inputs, tokenizer.eos_token_id)

        inputs["query_id"] = query_id

        for k, v in inputs.items():
            output[k] = v
        return output
    return _process


def evaluate_popqa(eval_data, save_path, **kwds):
    def compute_metric(eval_preds):
        makedirs(save_path)
        
        samples = {}
        with open(eval_data) as f:
            for line in f:
                sample = json.loads(line.strip())
                samples[sample["query_id"]] = sample

        accuracy = 0
        with open(save_path, "w") as f:
            for query_id, generation in zip(*eval_preds):
                sample = samples[query_id]
                answers = sample['possible_answers']
                correct = False
                for answer in answers:
                    # if any answer matches
                    if answer in generation or answer.lower() in generation or answer.capitalize() in generation:
                        correct = True
                        break

                accuracy += int(correct)

                sample["output"] = generation
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        accuracy /= len(eval_preds[0])
        return {"accuracy": accuracy}
    return compute_metric


def main():
    parser = HfArgumentParser([PopQAArgs, GenerationArgs])
    args, generation_args = parser.parse_args_into_dataclasses()
    
    accelerator = Accelerator(cpu=args.cpu)

    # modify the output_dir for retrieval
    if args.retrieval_method == "dense":
        output_dir = os.path.join(args.output_dir, args.query_encoder.strip(os.sep).replace(os.sep, "--"))
    else:
        output_dir = os.path.join(args.output_dir, args.retrieval_method)
    args.output_dir = output_dir

    if args.retrieval_method != "no":
        retrieval_main(args=args, accelerator=accelerator, log=False)
        eval_data = RetrievalMetric._get_save_path(args.eval_data, args.output_dir, field="key", save_name=args.save_name)
    else:
        eval_data = args.eval_data

    llm = LM(
        model_name_or_path=args.model_name_or_path,
        dtype=args.lm_dtype,
        device_map=args.lm_device_map,
        padding_side=args.padding_side,
        cache_dir=args.model_cache_dir,
        accelerator=accelerator,
        generation_args=asdict(generation_args)
    )

    tokenizer = llm.tokenizer

    logging.info(f"Loading data from {eval_data}...")

    with accelerator.main_process_first():
        dataset = datasets.load_dataset("json", data_files=eval_data, split="train", cache_dir=args.dataset_cache_dir)
        dataset = dataset.map(process_popqa(
            tokenizer, 
            context_max_length=args.context_max_length, 
            key_num=args.key_num,
            few_shot=args.few_shot,
            # popqa extracts few-shot examples from test data
            train_data=args.eval_data,
            cache_dir=args.dataset_cache_dir,
            is_encoder_decoder=llm.model.config.is_encoder_decoder
        ), remove_columns=dataset.column_names, batched=True, num_proc=32)

    data_collator = DefaultDataCollator(tokenizer=tokenizer, add_position_ids=args.add_position_ids)
    dataloader = DataLoader(
        dataset, 
        batch_size=args.lm_batch_size, 
        collate_fn=data_collator,
        pin_memory=True,
    )
    dataloader = accelerator.prepare(dataloader)

    results = llm.generate(dataloader)

    if accelerator.process_index == 0:
        file_logger = FileLogger(makedirs(args.log_path))
        result_path = os.path.join(args.output_dir, args.model_name_or_path.strip(os.sep).replace(os.sep, "--") + ".json")
        metrics = evaluate_popqa(eval_data, result_path)(results)
        file_logger.log(metrics, Args=asdict(args))

if __name__ == "__main__":
    main()
