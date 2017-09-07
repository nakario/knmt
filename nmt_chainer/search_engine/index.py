#!/usr/bin/env python

from whoosh.index import create_in, open_dir
from whoosh.fields import Schema, TEXT, ID
import os


"""index.py: Whoosh search engine's index"""
__author__ = "Ryota Nakao"
__license__ = "undecided"
__version__ = "1.0"
__email__ = "nakario@gmail.com"
__status__ = "Development"


def get_index(index_path, x, y, create_new=False):
    if create_new:
        schema = Schema(SRC=TEXT(stored=True), TGT=TEXT, ID=ID)
        ix = create_in(index_path, schema)
        writer = ix.writer()
        for i, (a, b) in enumerate(zip(x, y)):
            writer.add_document(SRC=a.strip(), TGT=b.strip(), ID=unicode(i))
        writer.commit()
    else:
        ix = open_dir(index_path)
    return ix
