#!/usr/bin/env python3
"""
Reads from stdin SQL queries separated by semicolon, and pretty-prints them to stdout (keeping the semicolons).
"""
import sys

import sqlparse


def main():
    print(sqlparse.format(sys.stdin.read(), reindent=True, keyword_case='upper'))


if __name__ == '__main__':
    main()
