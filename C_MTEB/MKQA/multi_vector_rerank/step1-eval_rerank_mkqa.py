"""
# 1. Rerank Search Results
python step0-rerank_results.py \
--encoder BAAI/bge-m3 \
--reranker BAAI/bge-m3 \
--languages ar fi ja ko ru es sv he th da de fr it nl pl pt hu vi ms km no tr zh_cn zh_hk zh_tw \
--search_result_save_dir ../dense_retrieval/search_results \
--qa_data_dir ../qa_data \
--rerank_result_save_dir ./rerank_results \
--top_k 100 \
--batch_size 4 \
--max_length 512 \
--pooling_method cls \
--normalize_embeddings True \
--dense_weight 1 --sparse_weight 0.3 --colbert_weight 1 \
--num_shards 1 --shard_id 0 --cuda_id 0

# 2. Print and Save Evaluation Results
python step1-eval_rerank_mkqa.py \
--encoder BAAI/bge-m3 \
--reranker BAAI/bge-m3 \
--languages ar fi ja ko ru es sv he th da de fr it nl pl pt hu vi ms km no tr zh_cn zh_hk zh_tw \
--search_result_save_dir ./rerank_results \
--qa_data_dir ../qa_data \
--eval_result_save_dir ./eval_results \
--metrics recall@20 recall@100 \
--threads 32
"""
import os
import sys
import json
import datasets
import numpy as np
import pandas as pd
from tqdm import tqdm
import multiprocessing
from pprint import pprint
from dataclasses import dataclass, field
from transformers import HfArgumentParser

sys.path.append("..")

from utils.normalize_text import normalize
from utils.evaluation import evaluate_recall_qa


@dataclass
class EvalArgs:
    languages: str = field(
        default="en",
        metadata={'help': 'Languages to evaluate. Avaliable languages: en ar fi ja ko ru es sv he th da de fr it nl pl pt hu vi ms km no tr zh_cn zh_hk zh_tw', 
                  "nargs": "+"}
    )
    reranker: str = field(
        default='BAAI/bge-m3',
        metadata={'help': 'Name or path of reranker'}
    )
    encoder: str = field(
        default='BAAI/bge-m3',
        metadata={'help': 'Name or path of encoder'}
    )
    search_result_save_dir: str = field(
        default='./rerank_results',
        metadata={'help': 'Dir to saving search results. Search results path is `result_save_dir/{encoder}-{reranker}/{lang}.txt`'}
    )
    qa_data_dir: str = field(
        default='../qa_data',
        metadata={'help': 'Dir to qa data.'}
    )
    metrics: str = field(
        default="recall@20",
        metadata={'help': 'Metrics to evaluate. Avaliable metrics: recall@k', 
                  "nargs": "+"}
    )
    eval_result_save_dir: str = field(
        default='./reranker_evaluation_results',
        metadata={'help': 'Dir to saving evaluation results. Evaluation results will be saved to `eval_result_save_dir/{encoder}-{reranker}.json`'}
    )
    threads: int = field(
        default=1,
        metadata={"help": "num of evaluation threads. <= 1 means single thread"}
    )


def check_languages(languages):
    if isinstance(languages, str):
        languages = [languages]
    avaliable_languages = ['en', 'ar', 'fi', 'ja', 'ko', 'ru', 'es', 'sv', 'he', 'th', 'da', 'de', 'fr', 'it', 'nl', 'pl', 'pt', 'hu', 'vi', 'ms', 'km', 'no', 'tr', 'zh_cn', 'zh_hk', 'zh_tw']
    for lang in languages:
        if lang not in avaliable_languages:
            raise ValueError(f"Language `{lang}` is not supported. Avaliable languages: {avaliable_languages}")
    return languages


def compute_average(results: dict):
    average_results = {}
    for _, result in results.items():
        for metric, score in result.items():
            if metric not in average_results:
                average_results[metric] = []
            average_results[metric].append(score)
    for metric, scores in average_results.items():
        average_results[metric] = np.mean(scores)
    return average_results


def save_results(model_name: str, reranker_name: str, results: dict, save_path: str, eval_languages: list):
    try:
        results['average'] = compute_average(results)
    except:
        results['average'] = None
        pass
    pprint(results)
    if not os.path.exists(os.path.dirname(save_path)):
        os.makedirs(os.path.dirname(save_path))
    results_dict = {
        'reranker': reranker_name,
        'model': model_name,
        'results': results
    }
    with open(save_path, 'w', encoding='utf-8') as f:
        json.dump(results_dict, f, indent=4, ensure_ascii=False)
    print(f'Results of evaluating `{reranker_name}` on `{eval_languages}` based on `{model_name}` saved at `{save_path}`')


def get_corpus_dict():
    corpus_dict = {}
    corpus = datasets.load_dataset('BeIR/nq', 'corpus')['corpus']
    for data in tqdm(corpus, desc="Loading corpus"):
        _id = str(data['_id'])
        content = f"{data['title']}\n{data['text']}".lower()
        content = normalize(content)
        corpus_dict[_id] = content
    return corpus_dict


def get_qa_dict(qa_path: str):
    qa_dict = {}
    dataset = datasets.load_dataset('json', data_files=qa_path)['train']
    for data in dataset:
        qid = str(data['id'])
        answers = data['answers']
        qa_dict[qid] = answers
    return qa_dict


def get_search_result_dict(search_result_path: str, top_k: int=100):
    search_result_dict = {}
    flag = True
    for _, row in tqdm(pd.read_csv(search_result_path, sep=' ', header=None).iterrows(), desc="Loading search results"):
        qid = str(row.iloc[0])
        docid = str(row.iloc[2])
        rank = int(row.iloc[3])
        if qid not in search_result_dict:
            search_result_dict[qid] = []
            flag = False
        if rank > top_k:
            flag = True
        if flag:
            continue
        else:
            search_result_dict[qid].append(docid)
    return search_result_dict


def evaluate(corpus_dict: dict, qa_dict: dict, search_result_path: str, metrics: list):
    top_k = max([int(metric.split('@')[-1]) for metric in metrics])
    search_result_dict = get_search_result_dict(search_result_path, top_k=int(top_k))
    
    search_results = []
    ground_truths = []
    for qid, docid_list in tqdm(search_result_dict.items(), desc="Preparing to evaluate"):
        answers = qa_dict[qid]
        doc_list = [corpus_dict[docid] for docid in docid_list]
        search_results.append(doc_list)
        ground_truths.append(answers)
    
    results = {}
    metrics = sorted([metric.lower() for metric in metrics])
    for metric in metrics:
        metric, k = metric.split('@')
        k = int(k)
        assert metric in ['recall'], f"Metric `{metric}` is not supported."
        if metric == 'recall':
            results[f'Recall@{k}'] = evaluate_recall_qa(search_results, ground_truths, k=k)
    return results


def main():
    parser = HfArgumentParser([EvalArgs])
    eval_args = parser.parse_args_into_dataclasses()[0]
    eval_args: EvalArgs
    
    corpus_dict = get_corpus_dict()
    
    languages = check_languages(eval_args.languages)
    
    if 'checkpoint-' in os.path.basename(eval_args.encoder):
        eval_args.encoder = os.path.dirname(eval_args.encoder) + '_' + os.path.basename(eval_args.encoder)
    
    if 'checkpoint-' in os.path.basename(eval_args.reranker):
        eval_args.reranker = os.path.dirname(eval_args.reranker) + '_' + os.path.basename(eval_args.reranker)
    
    try:
        for sub_dir in ['colbert', 'sparse', 'dense', 'colbert+sparse+dense']:
            results = {}
            if eval_args.threads > 1:
                threads = min(len(languages), eval_args.threads)
                pool = multiprocessing.Pool(processes=threads)
                results_list = []
                for lang in languages:
                    print("*****************************")
                    print(f"Start evaluating {lang} ...")
                    qa_path = os.path.join(eval_args.qa_data_dir, f"{lang}.jsonl")
                    qa_dict = get_qa_dict(qa_path)
                    
                    search_result_save_dir = os.path.join(eval_args.search_result_save_dir, sub_dir, f"{os.path.basename(eval_args.encoder)}-{os.path.basename(eval_args.reranker)}")
                    search_result_path = os.path.join(search_result_save_dir, f"{lang}.txt")
                    
                    results_list.append(pool.apply_async(evaluate, args=(corpus_dict, qa_dict, search_result_path, eval_args.metrics)))
                pool.close()
                pool.join()
                for i, lang in enumerate(languages):
                    results[lang] = results_list[i].get()
            else:
                for lang in languages:
                    print("*****************************")
                    print(f"Start evaluating {lang} ...")
                    qa_path = os.path.join(eval_args.qa_data_dir, f"{lang}.jsonl")
                    qa_dict = get_qa_dict(qa_path)
                    
                    search_result_save_dir = os.path.join(eval_args.search_result_save_dir, sub_dir, f"{os.path.basename(eval_args.encoder)}-{os.path.basename(eval_args.reranker)}")
                    search_result_path = os.path.join(search_result_save_dir, f"{lang}.txt")
                    
                    result = evaluate(corpus_dict, qa_dict, search_result_path, eval_args.metrics)
                    results[lang] = result
            
            print("****************************")
            print(sub_dir + ":")
            save_results(
                model_name=eval_args.encoder,
                reranker_name=eval_args.reranker,
                results=results,
                save_path=os.path.join(eval_args.eval_result_save_dir, sub_dir, f"{os.path.basename(eval_args.encoder)}-{os.path.basename(eval_args.reranker)}.json"),
                eval_languages=languages
            )
    except:
        results = {}
        if eval_args.threads > 1:
            threads = min(len(languages), eval_args.threads)
            pool = multiprocessing.Pool(processes=threads)
            results_list = []
            for lang in languages:
                print("*****************************")
                print(f"Start evaluating {lang} ...")
                qa_path = os.path.join(eval_args.qa_data_dir, f"{lang}.jsonl")
                qa_dict = get_qa_dict(qa_path)
                
                search_result_save_dir = os.path.join(eval_args.search_result_save_dir, f"{os.path.basename(eval_args.encoder)}-{os.path.basename(eval_args.reranker)}")
                search_result_path = os.path.join(search_result_save_dir, f"{lang}.txt")
                
                results_list.append(pool.apply_async(evaluate, args=(corpus_dict, qa_dict, search_result_path, eval_args.metrics)))
            pool.close()
            pool.join()
            for i, lang in enumerate(languages):
                results[lang] = results_list[i].get()
        else:
            for lang in languages:
                print("*****************************")
                print(f"Start evaluating {lang} ...")
                qa_path = os.path.join(eval_args.qa_data_dir, f"{lang}.jsonl")
                qa_dict = get_qa_dict(qa_path)
                
                search_result_save_dir = os.path.join(eval_args.search_result_save_dir, f"{os.path.basename(eval_args.encoder)}-{os.path.basename(eval_args.reranker)}")
                search_result_path = os.path.join(search_result_save_dir, f"{lang}.txt")
                
                result = evaluate(corpus_dict, qa_dict, search_result_path, eval_args.metrics)
                results[lang] = result
        save_results(
            model_name=eval_args.encoder,
            reranker_name=eval_args.reranker,
            results=results,
            save_path=os.path.join(eval_args.eval_result_save_dir, f"{os.path.basename(eval_args.encoder)}-{os.path.basename(eval_args.reranker)}.json"),
            eval_languages=languages
        )
        
    print("==================================================")
    print("Finish generating evaluation results with following model and reranker:")
    print(eval_args.encoder)
    print(eval_args.reranker)

if __name__ == "__main__":
    main()
