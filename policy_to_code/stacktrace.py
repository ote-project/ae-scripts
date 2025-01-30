#!/usr/bin/env python3
import json
import gzip
import sys

def get_stacktrace(transcript_file_name: str) -> list[tuple]:
    ret = []
    with gzip.open(transcript_file_name, mode="rt") as f:
        transcript_list = [json.loads(line) for line in f]

    for transcript in transcript_list:
        assert "elements" in transcript # TODO(kerneyJ): make sure that elements is the only key in this dictionary
        elements = transcript["elements"]
        for element in elements:
            assert len(element.keys()) == 1
            key = list(element.keys())[0]
            if "stacktrace" not in element[key]:
                continue
            ret.append((key, element[key]["stacktrace"].split("\n"))) # There is other information that I am not including here

    return ret

def filter_for(string: str, stacktrace: list[str]):
    # filters out paths that do not contain substring string
    return [line for line in stacktrace if string.lower() in line.lower()]

def filter_from(string: str, stacktrace: list[str]):
    # filters out paths that do contain substring string
    return [line for line in stacktrace if string.lower() not in line.lower()]

def file_lookup(root: str, path):
    pass

if __name__ == "__main__":
    # TODO(kerneyj): make this parse sys.argv
    ret =get_stacktrace("/home/ubuntu/dse/logs/autolab-courses-index-2r-test_7/invocations/transcript-0.json.gz")
    pair = ret[0]
    # for pair in ret:
    typ = pair[0]
    trace = pair[1]
    for line in filter_from("rspec", trace):
        print(line)

