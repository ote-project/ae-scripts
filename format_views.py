#!/usr/bin/env python3
import sys


def main():
    all_content = sys.stdin.read()
    sqls = all_content.split(";")
    for sql in sqls:
        sql = sql.replace("\n", " ").strip()
        if sql:
            print(sql + ";")


if __name__ == "__main__":
    main()

