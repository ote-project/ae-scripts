#!/usr/bin/env python3
"""
Reads from stdin SQL queries separated by semicolon, and prints them to stdout one per line (keeping the semicolons).
"""
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
