#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑测试，不需要安装任何东西。

::

    python run_tests.py          # 简洁输出
    python run_tests.py -v       # 逐条列出

等价于 ``python -m unittest discover -s tests -t tests``，
只是顺手把 ``src`` 和 ``tests`` 塞进 sys.path，省掉 pip install -e。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'src')
TESTS = os.path.join(ROOT, 'tests')


def main():
    for path in (SRC, TESTS):
        if path not in sys.path:
            sys.path.insert(0, path)

    verbosity = 2 if '-v' in sys.argv[1:] or '--verbose' in sys.argv[1:] else 1
    suite = unittest.TestLoader().discover(TESTS, pattern='test_*.py',
                                           top_level_dir=TESTS)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
