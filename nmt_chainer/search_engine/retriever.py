#!/usr/bin/env python

from __future__ import division
from abc import ABCMeta, abstractmethod
from itertools import chain


"""retriever.py: The main class for retrieving translation pairs"""
__author__ = "Ryota Nakao"
__license__ = "undecided"
__version__ = "1.0"
__email__ = "nakario@gmail.com"
__status__ = "Development"


class BaseEngine:
    __metaclass__ = ABCMeta

    @abstractmethod
    def search(self, query):
        pass


class Retriever:
    def __init__(self, engine, similarity, limit=None, training=False):
        self.engine = engine
        self.similarity = similarity
        self.limit = limit
        self.training = training

    def retrieve(self, src):
        subset = self.engine.search(src)
        if self.training:
            subset = filter(lambda x: x[0] != src, subset)
        subset = self.__rerank(subset, src)
        R = []
        coverage = 0
        src_symbols = src.split(" ")
        for pair in subset:
            if self.limit is not None and self.limit >= len(R):
                break
            sentences = [pair_[0] for pair_ in R] + [pair[0]]
            symbols = flatten([s.split(" ") for s in sentences])
            c_tmp = sum([s in symbols for s in src_symbols]) / len(src_symbols)
            if c_tmp > coverage:
                coverage = c_tmp
                R.append(pair)
        return R

    def __rerank(self, pairs, src):
        return sorted(pairs, reverse=True, key=lambda pair: self.similarity(pair[0], src))


def flatten(x):
    return list(chain.from_iterable(x))
