"""
python step0-hybrid_search_results.py \
--model_name_or_path BAAI/bge-m3 \
--languages ar de en es fr hi it ja ko pt ru th zh \
--dense_search_result_save_dir ../dense_retrieval/search_results \
--sparse_search_result_save_dir ../sparse_retrieval/search_results \
--hybrid_result_save_dir ./search_results \
--top_k 1000 \
--dense_weight 0.2 --sparse_weight 0.8
"""
import os
import pandas as pd
from tqdm import tqdm
from dataclasses import dataclass, field
from transformers import HfArgumentParser


@dataclass
class EvalArgs:
    model_name_or_path: str = field(
        default='BAAI/bge-m3',
        metadata={'help': 'Name or path of model'}
    )
    languages: str = field(
        default="en",
        metadata={'help': 'Languages to evaluate. Avaliable languages: ar de en es fr hi it ja ko pt ru th zh', 
                  "nargs": "+"}
    )
    top_k: int = field(
        default=1000,
        metadata={'help': 'Use reranker to rerank top-k retrieval results'}
    )
    sparse_weight: float = field(
        default=0.8,
        metadata={'help': 'Hybrid weight of sparse score'}
    )
    dense_weight: float = field(
        default=0.2,
        metadata={'help': 'Hybrid weight of dense score'}
    )
    dense_search_result_save_dir: str = field(
        default='../dense_retrieval/search_results',
        metadata={'help': 'Dir to saving dense search results. Search results path is `dense_search_result_save_dir/{model_name_or_path}/{lang}.txt`'}
    )
    sparse_search_result_save_dir: str = field(
        default='../sparse_retrieval/search_results',
        metadata={'help': 'Dir to saving sparse search results. Search results path is `sparse_search_result_save_dir/{model_name_or_path}/{lang}.txt`'}
    )
    hybrid_result_save_dir: str = field(
        default='./search_results',
        metadata={'help': 'Dir to saving hybrid search results. Reranked results will be saved to `hybrid_result_save_dir/{model_name_or_path}/{lang}.txt`'}
    )


def check_languages(languages):
    if isinstance(languages, str):
        languages = [languages]
    avaliable_languages = ['ar', 'de', 'en', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'th', 'zh']
    for lang in languages:
        if lang not in avaliable_languages:
            raise ValueError(f"Language `{lang}` is not supported. Avaliable languages: {avaliable_languages}")
    return languages


def get_search_result_dict(search_result_path: str, top_k: int=1000):
    search_result_dict = {}
    flag = True
    for _, row in pd.read_csv(search_result_path, sep=' ', header=None).iterrows():
        qid = str(row.iloc[0])
        docid = row.iloc[2]
        rank = int(row.iloc[3])
        score = float(row.iloc[4])
        if qid not in search_result_dict:
            search_result_dict[qid] = []
            flag = False
        if rank > top_k:
            flag = True
        if flag:
            continue
        else:
            search_result_dict[qid].append((docid, score))
    return search_result_dict


def save_hybrid_results(sparse_search_result_dict: dict, dense_search_result_dict: dict, hybrid_result_save_path: str, top_k: int=1000, dense_weight: float=0.2, sparse_weight: float=0.8):
    if not os.path.exists(os.path.dirname(hybrid_result_save_path)):
        os.makedirs(os.path.dirname(hybrid_result_save_path))
    
    qid_list = list(set(sparse_search_result_dict.keys()) | set(dense_search_result_dict.keys()))
    hybrid_results_list = []
    for qid in tqdm(qid_list, desc="Hybriding dense and sparse scores"):
        results = {}
        if qid in sparse_search_result_dict:
            for docid, score in sparse_search_result_dict[qid]:
                score = score / 10000.
                results[docid] = score * sparse_weight
        if qid in dense_search_result_dict:
            for docid, score in dense_search_result_dict[qid]:
                if docid in results:
                    results[docid] = results[docid] + score * dense_weight
                else:
                    results[docid] = score * dense_weight
        hybrid_results = [(docid, score) for docid, score in results.items()]
        hybrid_results.sort(key=lambda x: x[1], reverse=True)
        
        hybrid_results_list.append(hybrid_results[:top_k])
    
    with open(hybrid_result_save_path, 'w', encoding='utf-8') as f:
        for qid, hybrid_results in tqdm(zip(qid_list, hybrid_results_list), desc="Saving hybrid search results"):
            for rank, docid_score in enumerate(hybrid_results):
                docid, score = docid_score
                line = f"{qid} Q0 {docid} {rank+1} {score:.6f} Faiss-&-Anserini"
                f.write(line + '\n')


def main():
    parser = HfArgumentParser([EvalArgs])
    eval_args = parser.parse_args_into_dataclasses()[0]
    eval_args: EvalArgs
    
    languages = check_languages(eval_args.languages)
    
    if os.path.basename(eval_args.model_name_or_path).startswith('checkpoint-'):
        eval_args.model_name_or_path = os.path.dirname(eval_args.model_name_or_path) + '_' + os.path.basename(eval_args.model_name_or_path)
    
    for lang in languages:
        print("**************************************************")
        print(f"Start hybrid search results of {lang} ...")
        
        hybrid_result_save_path = os.path.join(eval_args.hybrid_result_save_dir, f"{os.path.basename(eval_args.model_name_or_path)}", f"{lang}.txt")
        
        sparse_search_result_save_dir = os.path.join(eval_args.sparse_search_result_save_dir, os.path.basename(eval_args.model_name_or_path))
        sparse_search_result_path = os.path.join(sparse_search_result_save_dir, f"{lang}.txt")
        
        sparse_search_result_dict = get_search_result_dict(sparse_search_result_path, top_k=eval_args.top_k)
        
        dense_search_result_save_dir = os.path.join(eval_args.dense_search_result_save_dir, os.path.basename(eval_args.model_name_or_path))

        dense_search_result_path = os.path.join(dense_search_result_save_dir, f"{lang}.txt")
        
        dense_search_result_dict = get_search_result_dict(dense_search_result_path, top_k=eval_args.top_k)
        
        save_hybrid_results(
            sparse_search_result_dict=sparse_search_result_dict, 
            dense_search_result_dict=dense_search_result_dict, 
            hybrid_result_save_path=hybrid_result_save_path,
            top_k=eval_args.top_k,
            sparse_weight=eval_args.sparse_weight,
            dense_weight=eval_args.dense_weight
        )
    
    print("==================================================")
    print("Finish generating reranked results with following model:")
    print(eval_args.model_name_or_path)


if __name__ == "__main__":
    main()
