#!/usr/bin/env python3
import json
import gzip
import sys

class PathObject(object):

    def __init__(self, stacktrace):
        self.pred = None # predecessor path object
        self.succ = None # successor path object
        self.stacktrace = stacktrace

        self.lineno = None
        self.file_path = None
        self.func_name = None
        self.locs = []

    def extract_fileline(self, application_name):
        # This parses the lines of stacktrace in ruby, if we ever try
        # different langauges than this might need to be changed
        if not self.stacktrace:
            return None
        split = None
        found = False
        for l in self.stacktrace:
            split = l.split(":")
            fp = split[-3]
            # Look for a stacktrace line within the application
            # that does not contain our code
            if application_name in fp and "/spec" not in fp:
                found = True
                self.locs.append((fp, split[-2]))
                # break

        self.file_path = split[-3]
        self.lineno = int(split[-2])
        self.func_name = split[-1].split("`")[1][:-1]
        return found

class PCAtom(PathObject):

    def __init__(self, cond, outcome, stacktrace):
        PathObject.__init__(self, stacktrace)
        self.cond = cond
        self.outcome = outcome

    def __str__(self):
        return f"cond: {self.cond}; outcome: {self.outcome}"

class QueryDecl(PathObject):

    def __init__(self, qid, query, params, stacktrace):
        PathObject.__init__(self, stacktrace)
        self.qid = qid
        self.query = query
        self.params = params

    def __str__(self):
        return f"query: {self.query}; params: {self.params}"

# These two classes I think are not needed
class QueryResRowDecl(PathObject):

    def __init__(self):
        pass # TODO(kerneyj): implement this for "sqlQueryResRowDecl" option in parse_element

class QueryResEnd(PathObject):

    def __init__(self):
        pass # TODO(kerneyj): implement this for "sqlQueryResEnd" option in parse_element

def parse_element(element: dict) -> PathObject:
    assert len(element.keys()) == 1
    key = list(element.keys())[0]
    if key == "pcAtom":
        return PCAtom(element[key]["cond"], element[key]["outcome"] if "outcome" in element[key] else None, element[key]["stacktrace"].split("\n"))
    elif key == "sqlQueryDecl":
        return QueryDecl(element[key]["qid"] if "qid" in element[key] else None, element[key]["query"], element[key]["params"], element[key]["stacktrace"].split("\n"))
    elif key == "sqlQueryResRowDecl":
        return;
    elif key == "sqlQueryResEnd":
        return;
    else:
        raise Exception(f"Found element type {key} that does not have an associated class\n{element[key].keys()}")

def link_pathobjects(list):
    # TODO(kerneyj): Wen mentioned that its a chain so just link them together
    # before doing this will need to implement all the above PathObjects
    pass

def parse_transcript(transcript_file_name: str) -> list[PathObject]:
    ret = []
    if ".gz" in transcript_file_name:
        with gzip.open(transcript_file_name, mode="r") as f:
            transcript_list = [json.loads(line) for line in f]
    else:
        with open(transcript_file_name, mode="r") as f:
            transcript_list = [json.loads(line) for line in f]

    for transcript in transcript_list:
        assert len(transcript.keys())
        elements = transcript["elements"]
        for element in elements:
            pa = parse_element(element)
            ret.append(pa)

    return ret

def filter_for(string: str, stacktrace: list[str]):
    # filters out paths that do not contain substring string
    return [line for line in stacktrace if string.lower() in line.lower()]

def filter_from(string: str, stacktrace: list[str]):
    # filters out paths that do contain substring string
    return [line for line in stacktrace if string.lower() not in line.lower()]

def line_lookup(pa: PathObject, root_path: str, strip_from_path: int = 0):
    # strip_from_path exists because the runs are in docker containers
    # and thus the location of the libraries and run time is different
    # so we will remove the first n characters from path string and
    file_path = pa.file_path[strip_from_path:]
    path = root_path + file_path
    with open(path, "r") as f:
        lines = [line for line in f]
    return lines[pa.lineno-1] # minus one because 0 base

if __name__ == "__main__":
    # TODO(kerneyj): make this parse sys.argv
    ret = parse_transcript("/home/ubuntu/dse/policy-extraction-scripts/policy_to_code/transcript-4893.json") # "/home/ubuntu/dse/logs/autolab-metrics-pending-2r-test_9/invocations/transcript-2.json.gz")
    s = set()
    for i, pa in enumerate(ret):
        if not pa:
            continue
        pa.extract_fileline("Autolab")
        for l in pa.locs:
            s.add(l)
        #if not pa.extract_fileline("Autolab"):
        #    continue
        # print(pa.file_path, pa.func_name, pa.lineno)

        #print(f"{i}, {type(pa)}" + (100 * '-'))
        #print(type(pa), pa.file_path)
        #for l in pa.stacktrace:
        #    print(l)
        #print(100 * '-')

        #if not isinstance(pa, QueryDecl):
        #    continue
        #if ret:
        #    if "/opt" in pa.file_path:
        #        line = line_lookup(pa, "/home/ubuntu/dse/", 4)
        #        print(pa.lineno, line, end="")
        #else:
        #    print()

    for l in s:
        print(l)
    print(f"Number of path objects {len(ret)}")
