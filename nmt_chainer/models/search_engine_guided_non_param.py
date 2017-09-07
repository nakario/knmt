import logging
logging.basicConfig()
log = logging.getLogger("rnns:models")
log.setLevel(logging.INFO)


def create_reference_memory(encdec, retrieved_pairs):
    reference_memory = []
    for idx_ex_src, idx_ex_tgt in retrieved_pairs:
        reference_memory.extend(encdec.compute_reference_memory(idx_ex_src, idx_ex_tgt))
    return reference_memory
