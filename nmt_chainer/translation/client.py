#!/usr/bin/env python
"""client.py: Client that can issue requests to KNMT Server."""
from __future__ import absolute_import, division, print_function, unicode_literals
__author__ = "Frederic Bergeron"
__license__ = "undecided"
__version__ = "1.0"
__email__ = "bergeron@pa.jst.jp"
__status__ = "Development"

import socket
import os.path
import re
from xml.sax.saxutils import escape

class Client(object):

    def __init__(self, server_ip, server_port):
        self.ip = server_ip
        self.port = server_port

    def query(self,
              sentence,
              article_id=1,
              beam_width=30,
              nb_steps=50,
              nb_steps_ratio=1.5,
              beam_score_length_normalization='none',
              beam_score_length_normalization_strength=0.2,
              post_score_length_normalization='simple',
              post_score_length_normalization_strength=0.2,
              beam_score_coverage_penalty='none',
              beam_score_coverage_penalty_strength=0.2,
              post_score_coverage_penalty='none',
              post_score_coverage_penalty_strength=0.2,
              prob_space_combination=False,
              normalize_unicode_unk=True,
              remove_unk=False,
              attempt_to_relocate_unk_source=False,
              sentence_id=1):

        query = """<?xml version="1.0" encoding="utf-8"?>
<article id="{0}"
    beam_width="{1}"
    nb_steps="{2}"
    nb_steps_ratio="{3}"
    beam_score_length_normalization="{4}"
    beam_score_length_normalization_strength="{5}"
    post_score_length_normalization="{6}"
    post_score_length_normalization_strength="{7}"
    beam_score_coverage_penalty="{8}"
    beam_score_coverage_penalty_strength="{9}"
    post_score_coverage_penalty="{10}"
    post_score_coverage_penalty_strength="{11}"
    prob_space_combination="{12}"
    normalize_unicode_unk="{13}"
    remove_unk="{14}"
    attempt_to_relocate_unk_source="{15}">
    <sentence id="{16}">
        <i_sentence>{17}</i_sentence>
    </sentence>
</article>"""

        query = query.format(article_id,
                             beam_width,
                             nb_steps,
                             nb_steps_ratio,
                             beam_score_length_normalization,
                             beam_score_length_normalization_strength,
                             post_score_length_normalization,
                             post_score_length_normalization_strength,
                             beam_score_coverage_penalty,
                             beam_score_coverage_penalty_strength,
                             post_score_coverage_penalty,
                             post_score_coverage_penalty_strength,
                             str(prob_space_combination).lower(),
                             str(normalize_unicode_unk).lower(),
                             str(remove_unk).lower(),
                             str(attempt_to_relocate_unk_source).lower(),
                             sentence_id,
                             escape(sentence))

        s = socket.socket()
        s.connect((self.ip, self.port))
        s.send(query.encode('utf-8'))

        try:
            resp = bytearray()
            while True:
                data = s.recv(1024)
                if not data:
                    break
                resp += data
            return resp.decode('utf-8')
        finally:
            s.close()
