#!/usr/bin/env python
"""models.py: Implementation of RNNSearch in Chainer"""
__author__ = "Fabien Cromieres"
__license__ = "undecided"
__version__ = "1.0"
__email__ = "fabien.cromieres@gmail.com"
__status__ = "Development"

from _collections import defaultdict
from operator import itemgetter
import numpy as np
import chainer
from chainer import cuda, Function, gradient_check, Variable, optimizers, serializers, utils
from chainer import Link, Chain, ChainList
import chainer.functions as F
import chainer.links as L
import math

import logging
logging.basicConfig()
log = logging.getLogger("rnns:models")
log.setLevel(logging.INFO)

class Encoder(Chain):
    """ Chain that encode a sequence. 
        The __call_ takes 2 parameters: sequence and mask.
        mask and length should be 2 python lists of same length #length.
        
        sequence should be a python list of Chainer Variables wrapping a numpy/cupy array of shape (mb_size,) and type int32 each.
            -- where mb_size is the minibatch size
        sequence[i].data[j] should be the jth element of source sequence number i, or a padding value if the sequence number i is
            shorter than j.
        
        mask should be a python list of Chainer Variables wrapping a numpy/cupy array of shape (mb_size,) and type bool each.
        mask[i].data[j] should be True if and only if sequence[i].data[j] is not a padding value.
        
        Return a chainer variable of shape (mb_size, #length, 2*Hi) and type float32
    """
    def __init__(self, Vi, Ei, Hi):
        super(Encoder, self).__init__(
            emb = L.EmbedID(Vi, Ei),
            gru_f = L.GRU(Hi, Ei),
            gru_b = L.GRU(Hi, Ei)
        )
        self.Hi = Hi
        self.add_param("initial_state_f", (1, Hi))
        self.add_param("initial_state_b", (1, Hi))

        self.initial_state_f.data[...] = np.random.randn(Hi)
        self.initial_state_b.data[...] = np.random.randn(Hi)
        
    def __call__(self, sequence, mask):
        
        mb_size = sequence[0].data.shape[0]
        
        mb_initial_state_f = F.broadcast_to(F.reshape(self.initial_state_f, (1, self.Hi)), (mb_size, self.Hi))
        mb_initial_state_b = F.broadcast_to(F.reshape(self.initial_state_b, (1, self.Hi)), (mb_size, self.Hi))
        
        embedded_seq = []
        for elem in sequence:
            embedded_seq.append(self.emb(elem))
            
#         self.gru_f.reset_state()
        prev_state = mb_initial_state_f
        forward_seq = []
        for x in embedded_seq:
            prev_state = self.gru_f(prev_state, x)
            forward_seq.append(prev_state)
            
#         self.gru_b.reset_state()
        prev_state = mb_initial_state_b
        backward_seq = []
        for pos, x in reversed(list(enumerate(embedded_seq))):
            prev_state = F.where(F.broadcast_to(
                                F.reshape(mask[pos], (mb_size, 1)), (mb_size, self.Hi)),
                                self.gru_b(prev_state, x), mb_initial_state_b) #TODO: optimize?
            backward_seq.append(prev_state)
        
        assert len(backward_seq) == len(forward_seq)
        res = []
        for xf, xb in zip(forward_seq, reversed(backward_seq)):
            res.append(F.reshape(F.concat((xf, xb), 1), (-1, 1, 2 * self.Hi)))
        
        return F.concat(res, 1)
    
class AttentionModule(Chain):
    """ Attention Module for computing the current context during decoding. 
        The __call_ takes 2 parameters: fb_concat and mask.
        
        fb_concat should be the result of a call to Encoder.
        mask is as in the description of Encoder
               
        Return a chainer variable of shape (mb_size, Hi) and type float32
    """
    def __init__(self, Hi, Ha, Ho):
        super(AttentionModule, self).__init__(
            al_lin_h = L.Linear(Hi, Ha, nobias = False),
            al_lin_s = L.Linear(Ho, Ha, nobias = True),
            al_lin_o = L.Linear(Ha, 1, nobias = True)                                     
        )
        self.Hi = Hi
        self.Ha = Ha
        
    def __call__(self, fb_concat, mask):
        mb_size, nb_elems, Hi = fb_concat.data.shape
        assert Hi == self.Hi
        precomputed_al_factor = F.reshape(self.al_lin_h(
                        F.reshape(fb_concat, (mb_size * nb_elems, self.Hi)))
                                          , (mb_size, nb_elems, self.Ha))
        
        concatenated_mask = F.concat([F.reshape(mask_elem, (mb_size, 1)) for mask_elem in mask], 1)
        
        def compute_ctxt(previous_state):
            current_mb_size = previous_state.data.shape[0]
            if current_mb_size < mb_size:
                al_factor, _ = F.split_axis(precomputed_al_factor, (current_mb_size,), 0)
                used_fb_concat, _ = F.split_axis(fb_concat, (current_mb_size,), 0)
                used_concatenated_mask, _ = F.split_axis(concatenated_mask, (current_mb_size,), 0)
            else:
                al_factor = precomputed_al_factor
                used_fb_concat = fb_concat
                used_concatenated_mask = concatenated_mask
                
            state_al_factor = self.al_lin_s(previous_state)
            state_al_factor_bc = F.broadcast_to(F.reshape(state_al_factor, (current_mb_size, 1, self.Ha)), (current_mb_size, nb_elems, self.Ha) )
            a_coeffs = F.reshape(self.al_lin_o(F.reshape(F.tanh(state_al_factor_bc + al_factor), 
                            (current_mb_size* nb_elems, self.Ha))), (current_mb_size, nb_elems))
            
            
            with cuda.get_device(used_concatenated_mask.data):
                a_coeffs = a_coeffs - 10000 * (1-used_concatenated_mask.data) 
            
            attn = F.softmax(a_coeffs)
            
            ci = F.reshape(F.batch_matmul(attn, used_fb_concat, transa = True), (current_mb_size, self.Hi))
            
            return ci, attn
        
        return compute_ctxt
    
    def compute_ctxt_demux(self, fb_concat, mask):
        mb_size, nb_elems, Hi = fb_concat.data.shape
        assert Hi == self.Hi
        assert mb_size == 1
        
        precomputed_al_factor = F.reshape(self.al_lin_h(
                        F.reshape(fb_concat, (mb_size * nb_elems, self.Hi)))
                                          , (mb_size, nb_elems, self.Ha))
        
        concatenated_mask = F.concat([F.reshape(mask_elem, (mb_size, 1)) for mask_elem in mask], 1)  
        
        def compute_ctxt(previous_state):
            current_mb_size = previous_state.data.shape[0]
                
            al_factor = F.broadcast_to(precomputed_al_factor, (current_mb_size, nb_elems, self.Ha))
#             used_fb_concat = F.broadcast_to(fb_concat, (current_mb_size, nb_elems, Hi))
            used_concatenated_mask = F.broadcast_to(concatenated_mask, (current_mb_size, nb_elems))
                
            state_al_factor = self.al_lin_s(previous_state)
            state_al_factor_bc = F.broadcast_to(F.reshape(state_al_factor, (current_mb_size, 1, self.Ha)), (current_mb_size, nb_elems, self.Ha) )
            a_coeffs = F.reshape(self.al_lin_o(F.reshape(F.tanh(state_al_factor_bc + al_factor), 
                            (current_mb_size* nb_elems, self.Ha))), (current_mb_size, nb_elems))
            
            
            with cuda.get_device(used_concatenated_mask.data):
                a_coeffs = a_coeffs - 10000 * (1-used_concatenated_mask.data) 
            
            attn = F.softmax(a_coeffs)
            
#             ci = F.reshape(F.batch_matmul(attn, used_fb_concat, transa = True), (current_mb_size, self.Hi))
            
            ci = F.reshape(F.matmul(attn, F.reshape(fb_concat, (nb_elems, Hi))), (current_mb_size, self.Hi))
            
            return ci, attn
        
        return compute_ctxt
          
class Decoder(Chain):
    """ Decoder for RNNSearch. 
        The __call_ takes 3 required parameters: fb_concat, targets, mask.
        
        fb_concat should be the result of a call to Encoder.
        
        targets is a python list of chainer variables of type int32 and of variable shape (n,)
            the values n should be decreasing:
                i < j => targets[i].data.shape[0] >= targets[j].data.shape[0]
            targets[i].data[j] is the jth elements of the ith sequence in the minibatch
            all this imply that the sequences of the minibatch should be sorted from longest to shortest
            
        mask is as in the description of Encoder.
        
        * it is up to the user to add an EOS token to the data.
               
        Return a loss and the attention model values
    """
    def __init__(self, Vo, Eo, Ho, Ha, Hi, Hl):
        super(Decoder, self).__init__(
            emb = L.EmbedID(Vo, Eo),
            gru = L.GRU(Ho, Eo + Hi),
            
            maxo = L.Maxout(Eo + Hi + Ho, Hl, 2),
            lin_o = L.Linear(Hl, Vo, nobias = False),
            
            attn_module = AttentionModule(Hi, Ha, Ho)
        )
        self.add_param("initial_state", (1, Ho))
        self.add_param("bos_embeding", (1, Eo))
        self.Hi = Hi
        self.Ho = Ho
        self.Eo = Eo
        self.initial_state.data[...] = np.random.randn(Ho)
        self.bos_embeding.data[...] = np.random.randn(Eo)
        
    def advance_one_step(self, previous_state, prev_y, compute_ctxt):

        ci, attn = compute_ctxt(previous_state)
        concatenated = F.concat( (prev_y, ci) )
#             print concatenated.data.shape
        new_state = self.gru(previous_state, concatenated)

        all_concatenated = F.concat((concatenated, new_state))
        logits = self.lin_o(self.maxo(all_concatenated))
        
        return new_state, logits, attn
          
    def sample(self, nb_steps, compute_ctxt, mb_size, best = False, keep_attn_values = False,
               need_score = False):
        previous_state = F.broadcast_to(self.initial_state, (mb_size, self.Ho))
#         previous_word = Variable(np.array([self.bos_idx] * mb_size, dtype = np.int32))
        xp = cuda.get_array_module(self.initial_state.data)
        
        previous_word = None
        with cuda.get_device(self.initial_state.data):
#             previous_word = Variable(xp.array([self.bos_idx] * mb_size, dtype = np.int32))
            prev_y = F.broadcast_to(self.bos_embeding, (mb_size, self.Eo))
        score = 0
        sequences = []
        attn_list = []
        for i in xrange(nb_steps):
#             print "i", i
            if previous_word is not None: #else we are using the initial prev_y
                prev_y = self.emb(previous_word)
            new_state, logits, attn = self.advance_one_step(previous_state, prev_y, 
                                                      compute_ctxt)
            if keep_attn_values:
                attn_list.append(attn)
#             print logits.data.shape
            probs = F.softmax(logits)
            if best:
                curr_idx = xp.argmax(probs.data, 1).astype(np.int32)
            else:
                curr_idx = xp.empty((mb_size,), dtype = np.int32)
                probs_data = cuda.to_cpu(probs.data)
                for i in xrange(mb_size):
                    sampler = chainer.utils.WalkerAlias(probs_data[i])
                    curr_idx[i] =  sampler.sample(1)[0]
            if need_score:
                score = score + np.log(cuda.to_cpu(probs.data)[np.arange(mb_size),cuda.to_cpu(curr_idx)])
            sequences.append(curr_idx)
            
            previous_word = Variable(curr_idx, volatile = "auto")
            previous_state = new_state
            
        return sequences, score, attn_list
    
    def beam_search(self, fb_concat, mask, nb_steps, eos_idx, beam_width = 20):
        mb_size, nb_elems, Hi = fb_concat.data.shape
        assert Hi == self.Hi, "%i != %i"%(Hi, self.Hi)
        compute_ctxt = self.attn_module(fb_concat, mask)
        
        assert mb_size == 1
        finished_translations = []
        current_translations = [(F.reshape(self.initial_state, (1, -1)), [([], 0.0)])]
        xp = cuda.get_array_module(self.initial_state.data)
        for i in xrange(nb_steps):
            next_translations = []
            for current_state, candidates in current_translations:
                ci, attn = compute_ctxt(current_state)
                for t, score in candidates:
                    if len(t) > 0:
                        with cuda.get_device(self.initial_state.data):
                            prev_w = xp.array([t[-1]], dtype = xp.int32)
                        prev_w_v = Variable(prev_w, volatile = "auto")
                        prev_y = self.emb(prev_w_v)
                    else:
                        prev_y = F.reshape(self.bos_embeding, (1, -1))
                
                    concatenated = F.concat( (prev_y, ci) )
                    new_state = self.gru(current_state, concatenated)
                
                    all_concatenated = F.concat((concatenated, new_state))
                    logits = self.lin_o(self.maxo(all_concatenated))
                
                    probs = cuda.to_cpu(F.softmax(logits).data).reshape((-1,))
                    best_idx = np.argpartition(- probs, beam_width)[:beam_width].astype(np.int32)
                    
                    cand_list = []
                    for num in xrange(len(best_idx)):
                        idx = best_idx[num]
                        sc = np.log(probs[idx])
                        if idx == eos_idx:
                            finished_translations.append((t, score + sc))
                        else:
                            cand_list.append((t + [idx], score + sc))
                
                    next_translations.append((new_state, cand_list))
                
            # pruning
            coord_next_t = []
            for num_st in xrange(len(next_translations)):
                for num_cand in xrange(len(next_translations[num_st][1])):
                    score = next_translations[num_st][1][num_cand][1]
                    coord_next_t.append((score, num_st, num_cand))
            coord_next_t.sort(reverse = True)
            next_translations_pruned = []
            
            next_translations_pruned_dict = defaultdict(list)
            for score, num_st, num_cand in coord_next_t[:beam_width]:
                next_translations_pruned_dict[num_st].append(num_cand)
                
            next_translations_pruned = []
            for num_st, num_cand_list in  next_translations_pruned_dict.iteritems():
                state = next_translations[num_st][0]
                cand_list = []
                for num_cand in num_cand_list:
                    cand_list.append(next_translations[num_st][1][num_cand])
                next_translations_pruned.append((state, cand_list))
            current_translations = next_translations_pruned
        if len (finished_translations) == 0:
            finished_translations.append(([], 0))
        return finished_translations
    
    def beam_search_opt(self, fb_concat, mask, nb_steps, eos_idx, beam_width = 20):
        mb_size, nb_elems, Hi = fb_concat.data.shape
        assert Hi == self.Hi, "%i != %i"%(Hi, self.Hi)
        compute_ctxt = self.attn_module.compute_ctxt_demux(fb_concat, mask)
        
        assert mb_size == 1
        finished_translations = []
        current_translations_states = (
                                [[]], 
                                np.array([0]),
                                F.reshape(self.initial_state, (1, -1)), 
                                None
                                )
        xp = cuda.get_array_module(self.initial_state.data)
        for i in xrange(nb_steps):
            current_translations, current_scores, current_states, current_words = current_translations_states
            
#             if current_states.data.shape[0] > 1:
#                 nb_st = current_states.data.shape[0]
#                 ci_list = []
#                 for st in F.split_axis(current_states, nb_st, axis = 0):
#                     ci0, attn = compute_ctxt(st)
#                     ci_list.append(ci0)
#                 ci = F.concat(ci_list, 0)
#             else:
            ci, attn = compute_ctxt(current_states)
            if current_words is not None:
#                 with cuda.get_device(self.initial_state.data):
#                     prev_w = xp.array([t[-1]], dtype = xp.int32)
#                 prev_w_v = Variable(prev_w, volatile = "auto")
                prev_y = self.emb(current_words)
            else:
                prev_y = F.reshape(self.bos_embeding, (1, -1))
                    
            concatenated = F.concat( (prev_y, ci) )
            new_state = self.gru(current_states, concatenated)
        
            all_concatenated = F.concat((concatenated, new_state))
            logits = self.lin_o(self.maxo(all_concatenated))
            probs_v = F.softmax(logits)
            log_probs_v = F.log(probs_v) # TODO replace wit a logsoftmax if implemented
            nb_cases, v_size = probs_v.data.shape
            assert nb_cases <= beam_width
            
            new_scores = current_scores[:, np.newaxis] + cuda.to_cpu(log_probs_v.data)
            new_scores_flattened =  new_scores.flatten()
#             best_idx = np.argpartition( - probs_flattened, beam_width)[:beam_width]
#             best_idx = np.argsort( - new_scores_flattened)
            best_idx = np.argpartition( - new_scores_flattened, beam_width)[:beam_width]
            
            
            next_states_list = []
            next_words_list = []
            next_score_list = []
            next_translations_list = []
            for num in xrange(len(best_idx)):
                idx = best_idx[num]
                num_case = idx / v_size
                idx_in_case = idx % v_size
                if idx_in_case == eos_idx:
                    finished_translations.append((current_translations[num_case], 
                                                  new_scores_flattened[idx]))
                else:
                    next_states_list.append(Variable(new_state.data[num_case].reshape(1,-1), volatile = "auto"))
                    next_words_list.append(idx_in_case)
                    next_score_list.append(new_scores_flattened[idx])
                    next_translations_list.append(current_translations[num_case] + [idx_in_case])
                    if len(next_states_list) >= beam_width:
                        break
                
            if len(next_states_list) == 0:
                break
            current_translations_states = (next_translations_list,
                                        np.array(next_score_list),
                                        F.concat(next_states_list, axis = 0),
                                        Variable(cuda.to_gpu(np.array(next_words_list, dtype = np.int32)), volatile = "auto")
                                        )
            
        if len (finished_translations) == 0:
            finished_translations.append(([], 0))
        return finished_translations
    
    
    def compute_loss(self, targets, compute_ctxt, raw_loss_info = False, keep_attn_values = False):
        loss = None
        current_mb_size = targets[0].data.shape[0]
#         previous_state = F.concat( [self.initial_state] * current_mb_size, 0)
        previous_state = F.broadcast_to(self.initial_state, (current_mb_size, self.Ho))
#         previous_word = Variable(np.array([self.bos_idx] * mb_size, dtype = np.int32))
        xp = cuda.get_array_module(self.initial_state.data)
        previous_word = None
        with cuda.get_device(self.initial_state.data):
#             previous_word = Variable(xp.array([self.bos_idx] * current_mb_size, dtype = np.int32))
            prev_y = F.broadcast_to(self.bos_embeding, (current_mb_size, self.Eo))
        attn_list = []
        total_nb_predictions = 0
        for i in xrange(len(targets)):
            assert i == 0 or previous_state.data.shape[0] == previous_word.data.shape[0]
            current_mb_size = targets[i].data.shape[0]
            if current_mb_size < len(previous_state.data):
                previous_state, _ = F.split_axis(previous_state, (current_mb_size,), 0)
                if previous_word is not None:
                    previous_word, _ = F.split_axis(previous_word, (current_mb_size,), 0 )
            if previous_word is not None: #else we are using the initial prev_y
                prev_y = self.emb(previous_word)
            assert previous_state.data.shape[0] == current_mb_size
            
            new_state, logits, attn = self.advance_one_step(previous_state, prev_y, 
                                                      compute_ctxt)

            if keep_attn_values:
                attn_list.append(attn)
                
            local_loss = F.softmax_cross_entropy(logits, targets[i])   
            
            total_nb_predictions += current_mb_size
            total_local_loss = local_loss * current_mb_size
            
#             loss = local_loss if loss is None else loss + local_loss
            loss = total_local_loss if loss is None else loss + total_local_loss
            
            previous_word = targets[i]
#             prev_y = self.emb(previous_word)
            previous_state = new_state
#             attn_list.append(attn)
        if raw_loss_info:
            return (loss, total_nb_predictions), attn_list
        else:
            loss = loss / total_nb_predictions
            return loss, attn_list
    
    def __call__(self, fb_concat, targets, mask, use_best_for_sample = False, raw_loss_info = False,
                    keep_attn_values = False, need_score = False):
        mb_size, nb_elems, Hi = fb_concat.data.shape
        assert Hi == self.Hi, "%i != %i"%(Hi, self.Hi)
    
        compute_ctxt = self.attn_module(fb_concat, mask)

        if isinstance(targets, int):
            return self.sample(targets, compute_ctxt, mb_size, best = use_best_for_sample,
                               keep_attn_values = keep_attn_values, need_score = need_score)
        else:
            return self.compute_loss(targets, compute_ctxt, raw_loss_info = raw_loss_info,
                                     keep_attn_values = keep_attn_values)     
        
class EncoderDecoder(Chain):
    """ Do RNNSearch Encoding/Decoding
        The __call__ takes 3 required parameters: src_batch, tgt_batch, src_mask
        src_batch is as in the sequence parameter of Encoder
        tgt_batch is as in the targets parameter of Decoder
        src_mask is as in the mask parameter of Encoder
        
        return loss and attention values
    """
    def __init__(self, Vi, Ei, Hi, Vo, Eo, Ho, Ha, Hl):
        log.info("constructing encoder decoder with Vi:%i Ei:%i Hi:%i Vo:%i Eo:%i Ho:%i Ha:%i Hl:%i" % 
                                        (Vi, Ei, Hi, Vo, Eo, Ho, Ha, Hl))
        super(EncoderDecoder, self).__init__(
            enc = Encoder(Vi, Ei, Hi),
            dec = Decoder(Vo, Eo, Ho, Ha, 2 * Hi, Hl)
        )
        
    def __call__(self, src_batch, tgt_batch, src_mask, use_best_for_sample = False, display_attn = False,
                 raw_loss_info = False, keep_attn_values = False, need_score = False):
        fb_src = self.enc(src_batch, src_mask)
        loss = self.dec(fb_src, tgt_batch, src_mask, use_best_for_sample = use_best_for_sample, raw_loss_info = raw_loss_info,
                        keep_attn_values = keep_attn_values, need_score = need_score)
        return loss
    
    def sample(self, src_batch, src_mask, nb_steps, use_best_for_sample, keep_attn_values = False, need_score = False):
        fb_src = self.enc(src_batch, src_mask)
        samp = self.dec.sample(self, fb_src, nb_steps, src_mask, use_best_for_sample = use_best_for_sample,
                        keep_attn_values = keep_attn_values, need_score = need_score)
        return samp
    
    def beam_search(self, src_batch, src_mask, nb_steps, eos_idx, beam_width = 20, beam_opt = False):
        fb_src = self.enc(src_batch, src_mask)
        if beam_opt:
            return self.dec.beam_search_opt(fb_src, src_mask, nb_steps, eos_idx = eos_idx, beam_width = beam_width)
        else:
            return self.dec.beam_search(fb_src, src_mask, nb_steps, eos_idx = eos_idx, beam_width = beam_width)
        
        
        