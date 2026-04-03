import argparse
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

parser = argparse.ArgumentParser(description='eval el result')
parser.add_argument('-result_file', type=str, default=str(DATA_DIR / 'yago_new_rerank.result'))
parser.add_argument('-candidate_file', type=str, default=str(DATA_DIR / 'yago_new.candidate'))


def extract_predicted_name(text):
    # Matches text inside square brackets and captures it
    pattern = r'\[(.*?)\]'
    match = re.search(pattern, text)
    if match:
        extracted_text = match.group(1)
        return extracted_text
    else:return None

def chunk_by_can_num(mentions, candidate_file, interval=[5, 10, 20]):
    min_num, middle_num, max_num = interval[0], interval[1], interval[2]
    candidate_num = dict()
    for line in open(candidate_file, encoding='utf8'):
        row = line.strip().split('\t')
        mention, candidates = row[0], row[1:]
        candidate_num[mention] = len(candidates)

    group1, group2, group3, group4 = list(), list(), list(), list()
    for m in mentions:
        cnt = candidate_num[m]
        if cnt < min_num:
            group1.append(m)
        elif cnt >= min_num and cnt < middle_num:
            group2.append(m)
        elif cnt >= middle_num and cnt < max_num:
            group3.append(m)
        else:
            group4.append(m)
    return [group1, group2, group3, group4]


def cal_acc(result_file, candidate_file):
    def __acc(group):
        acc_cnt, total_cnt = 0, 0
        for line in open(result_file, encoding='utf8'):
            row = line.strip().split('\t')
            mention, predicted, label = row[1], row[2], row[3]
            if mention not in group:continue
            e_type = extract_predicted_name(predicted)
            if e_type:
                predicted = predicted.split(' [')[0]
            if predicted == label:
                acc_cnt += 1
            total_cnt += 1
        acc = acc_cnt * 1.0 / total_cnt
        return acc

    mentions = [line.strip().split('\t')[1] for line in open(result_file, encoding='utf8')][:19000]
    groups = chunk_by_can_num(mentions, candidate_file)
    total = 0
    acc_list = list()
    for index, group in enumerate(groups):
        acc = __acc(group)
        acc_list.append(acc)
        print("group [{a}]".format(a=index+1), len(group), acc)
        total += len(group)
    print("Macro", total, sum(acc_list)/len(acc_list))


if __name__ == "__main__":
    try:
        args = parser.parse_args()
    except:
        parser.print_help()
        sys.exit(0)
    cal_acc(args.result_file, args.candidate_file)
