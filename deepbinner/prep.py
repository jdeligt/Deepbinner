"""
Copyright 2018 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Deepbinner/

This file is part of Deepbinner. Deepbinner is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Deepbinner is distributed
in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Deepbinner.
If not, see <http://www.gnu.org/licenses/>.
"""

import edlib
import mappy as mp
import pathlib
import random
import re
import sys

from .load_fast5s import get_read_id_and_signal, find_all_fast5s
from .misc import load_fastq
from .trim_signal import normalise, find_signal_start_pos, CannotTrim
from .dtw import semi_global_dtw_with_rescaling
from . import sequences
from . import signals


MIN_ADAPTER_IDENTITY = 70.0
MIN_BARCODE_IDENTITY = 70.0
MIN_BEST_SECOND_BEST_DIFF = 7.5
MIN_REFERENCE_IDENTITY = 70.0
MIN_READ_COVERAGE = 70.0
ADAPTER_BARCODE_ACCEPTABLE_GAP = 4
ADAPTER_REFERENCE_ACCEPTABLE_GAP = 4
BARCODE_REFERENCE_ACCEPTABLE_GAP = 10

BARCODED_SAMPLES_PER_BARCODED_READ = 2
NON_BARCODED_SAMPLES_PER_NON_BARCODED_READ = 2
NON_BARCODED_SAMPLE_FROM_BEFORE_BARCODE = True


def prep(args):
    if pathlib.Path(args.fast5_dir).is_dir():
        fast5s = find_all_fast5s(args.fast5_dir)
    else:
        fast5s = [args.fast5_dir]

    read_seqs = load_fastq(args.fastq)

    # For the ligation kit we need to align to reference (but not for the rapid kit).
    if args.kit == 'EXP-NBD103':
        mappy_aligner = mp.Aligner(args.ref_fasta)
    else:
        mappy_aligner = None

    for fast5_file in fast5s:
        read_id, signal = get_read_id_and_signal(fast5_file)
        if read_id not in read_seqs:
            continue

        print('', file=sys.stderr)
        print(fast5_file, file=sys.stderr)
        print('  read ID: {}'.format(read_id), file=sys.stderr)

        if args.kit == 'EXP-NBD103' and args.start_end == 'start':
            prep_native_read_start(signal, read_seqs[read_id], mappy_aligner, args.signal_size)

        if args.kit == 'EXP-NBD103' and args.start_end == 'end':
            prep_native_read_end()

        elif args.kit == 'SQK-RBK004' and args.start_end == 'start':
            prep_rapid_read_start()


def prep_native_read_start(signal, basecalled_seq, mappy_aligner, signal_size):
    print('  sequence-based alignment', file=sys.stderr)

    ref_start, ref_end = align_read_to_reference(basecalled_seq, mappy_aligner)
    if ref_start is None:
        return

    basecalled_start = basecalled_seq[:500]
    adapter_seq_start, adapter_seq_end = align_adapter_to_read_seq(basecalled_start)
    if adapter_seq_start is None:
        return

    barcode_name, barcode_start, barcode_end = \
        get_best_barcode(basecalled_start, sequences.native_start_barcodes)

    print('  signal-based DTW alignment', file=sys.stderr)

    signal = trim_signal(signal)
    normalised_signal = normalise(signal)

    adapter_signal_start, adapter_signal_end = align_adapter_to_read_dtw(normalised_signal)
    if adapter_signal_start is None:
        return

    if barcode_name == 'too close':
        return

    elif barcode_name == 'none':
        if does_ref_follow_adapter(adapter_seq_end, ref_start):
            contains_barcode = False
        else:
            return

    else:  # barcode_name is 01, 02, 03, etc.
        contains_barcode = True

    if contains_barcode:
        if basecalled_elements_oddly_spaced(adapter_seq_end, barcode_start, barcode_end, ref_start):
            return

        barcode_search_signal_start = adapter_signal_end - 100
        barcode_search_signal = \
            normalised_signal[barcode_search_signal_start:adapter_signal_end + 1000]
        barcode_signal_start, barcode_signal_end = \
            align_barcode_to_read_dtw(barcode_search_signal, barcode_search_signal_start,
                                      barcode_name)
        if barcode_signal_start is None:
            return

        if signal_elements_oddly_spaced(adapter_signal_end, barcode_signal_start,
                                        barcode_signal_end):
            return

        make_barcoded_training_samples(barcode_name, adapter_seq_start, adapter_seq_end,
                                       barcode_start, barcode_end, ref_start, ref_end,
                                       adapter_signal_start, adapter_signal_end,
                                       barcode_signal_start, barcode_signal_end,
                                       signal, signal_size)

    else:
        make_non_barcoded_training_samples(adapter_seq_start, adapter_seq_end, ref_start,
                                           ref_end, adapter_signal_start, adapter_signal_end,
                                           signal, signal_size)


def prep_native_read_end():
    pass


def prep_rapid_read_start():
    pass


def align_read_to_reference(basecalled_seq, mappy_aligner):
    ref_id, read_cov, ref_start, ref_end = minimap_align(basecalled_seq, mappy_aligner)
    if ref_id == 0.0:
        print('  verdict: skipping due to no alignment to reference', file=sys.stderr)
        return None, None
    elif ref_id < MIN_REFERENCE_IDENTITY:
        print('  verdict: skipping due to low reference alignment identity', file=sys.stderr)
        return None, None
    elif read_cov < MIN_READ_COVERAGE:
        print('  verdict: skipping due to short reference alignment', file=sys.stderr)
        return None, None
    else:
        print('    reference seq: {}-{} ({:.1f}%)'.format(ref_start, ref_end, ref_id),
              file=sys.stderr)
        return ref_start, ref_end


def minimap_align(query_seq, aligner):
    best_hit, best_mlen = None, 0
    for hit in aligner.map(query_seq):
        if hit.mlen > best_mlen:
            best_hit, best_mlen = hit, hit.mlen
    if best_hit is None:
        return 0.0, 0.0, 0, 0
    identity = 100.0 * best_hit.mlen / best_hit.blen
    read_start, read_end = best_hit.q_st, best_hit.q_en
    read_cov = 100.0 * (read_end - read_start) / len(query_seq)
    return identity, read_cov, read_start, read_end


def align_adapter_to_read_seq(basecalled_start):
    adapter_identity, adapter_start, adapter_end = edlib_align(sequences.native_start_kit_adapter,
                                                               basecalled_start)
    print('    adapter seq: {}-{} ({:.1f}%)'.format(adapter_start, adapter_end, adapter_identity),
          file=sys.stderr)
    if adapter_identity < MIN_ADAPTER_IDENTITY:
        print('  verdict: skipping due to low adapter alignment identity', file=sys.stderr)
        return None, None
    else:
        return adapter_start, adapter_end


def trim_signal(signal):
    print('    untrimmed signal length: {}'.format(len(signal)), file=sys.stderr)
    try:
        start_trim_pos = find_signal_start_pos(signal)
    except CannotTrim:
        print('  verdict: skipping due to failed signal trimming', file=sys.stderr)
        return None
    print('    trim amount: {}'.format(start_trim_pos), file=sys.stderr)
    return signal[start_trim_pos:]


def align_adapter_to_read_dtw(signal):
    for i in range(0, 15000, 500):
        adapter_search_signal = signal[i:i+1500]
        if len(adapter_search_signal) > 0:
            adapter_distance, adapter_signal_start, adapter_signal_end, _ = \
                semi_global_dtw_with_rescaling(adapter_search_signal,
                                               signals.native_start_kit_adapter)
            adapter_signal_start += i
            adapter_signal_end += i
            if adapter_distance <= 50.0:
                print('    adapter DTW: {}-{} ({:.2f})'.format(adapter_signal_start,
                                                             adapter_signal_end, adapter_distance),
                      file=sys.stderr)
                return adapter_signal_start, adapter_signal_end
    else:
        print('  verdict: skipping due to high adapter DTW distance', file=sys.stderr)
        return None, None


def edlib_align(query_seq, ref_seq):
    alignment = edlib.align(query_seq, ref_seq, mode='HW', task='path')
    return (identity_from_edlib_cigar(alignment['cigar']),
            alignment['locations'][0][0], alignment['locations'][0][1])


def identity_from_edlib_cigar(cigar):
    matches, alignment_length = 0, 0
    cigar_parts = re.findall(r'\d+[IDX=]', cigar)
    for c in cigar_parts:
        cigar_type = c[-1]
        cigar_size = int(c[:-1])
        alignment_length += cigar_size
        if cigar_type == '=':
            matches += cigar_size
    try:
        return 100.0 * matches / alignment_length
    except ZeroDivisionError:
        return 0.0


def get_best_barcode(read_seq, barcode_seqs):
    best_barcode_name, best_barcode_identity = None, 0.0
    best_start, best_end = 0, 0
    all_identities = []
    for barcode_name, barcode_seq in barcode_seqs.items():
        barcode_identity, barcode_start, barcode_end = edlib_align(barcode_seq, read_seq)
        if barcode_identity > best_barcode_identity:
            best_barcode_name, best_barcode_identity = barcode_name, barcode_identity
            best_start, best_end = barcode_start, barcode_end
        all_identities.append(barcode_identity)
    all_identities = sorted(all_identities)
    best_second_best_diff = all_identities[-1] - all_identities[-2]
    if best_barcode_identity < MIN_BARCODE_IDENTITY:
        return 'none', best_start, best_end
    if best_second_best_diff < MIN_BEST_SECOND_BEST_DIFF:
        print('  verdict: skipping due to too-close-to-call barcodes', file=sys.stderr)
        return 'too close', best_start, best_end
    else:
        print('    best barcode: #{}, {}-{} ({:.2f}%)'.format(best_barcode_name, best_start,
                                                              best_end, best_barcode_identity),
              file=sys.stderr)
        return best_barcode_name, best_start, best_end


def does_ref_follow_adapter(adapter_seq_end, ref_start):
    if abs(adapter_seq_end - ref_start) <= ADAPTER_REFERENCE_ACCEPTABLE_GAP:
        return True
    else:
        print('  verdict: skipping due to odd adapter-reference arrangement', file=sys.stderr)
        return False


def basecalled_elements_oddly_spaced(adapter_seq_end, barcode_start, barcode_end, ref_start):
    # See if the arrangement of elements in the basecalled read looks too weird.
    if abs(adapter_seq_end - barcode_start) > ADAPTER_BARCODE_ACCEPTABLE_GAP:
        print('  verdict: skipping due to odd adapter-barcode arrangement', file=sys.stderr)
        return True

    # See if the arrangement of elements in the basecalled read looks too weird.
    if abs(barcode_end - ref_start) > BARCODE_REFERENCE_ACCEPTABLE_GAP:
        print('  verdict: skipping due to odd barcode-reference arrangement', file=sys.stderr)
        return True

    return False


def align_barcode_to_read_dtw(barcode_search_signal, barcode_search_signal_start, barcode_name):
    barcode_distance, barcode_signal_start, barcode_signal_end, _ = \
        semi_global_dtw_with_rescaling(barcode_search_signal,
                                       signals.native_start_barcodes[barcode_name])
    barcode_signal_start += barcode_search_signal_start
    barcode_signal_end += barcode_search_signal_start
    print('    barcode{} DTW: {}-{} ({:.2f})'.format(barcode_name, barcode_signal_start,
                                                     barcode_signal_end, barcode_distance),
          file=sys.stderr)
    if barcode_distance > 50.0:
        print('  verdict: skipping due to high barcode DTW distance', file=sys.stderr)
        return None, None
    else:
        return barcode_signal_start, barcode_signal_end


def signal_elements_oddly_spaced(adapter_signal_end, barcode_signal_start, barcode_signal_end):
    adapter_barcode_gap = barcode_signal_start - adapter_signal_end
    print('    adapter-barcode signal gap: {}'.format(adapter_barcode_gap), file=sys.stderr)
    print('    barcode signal size: {}'.format(barcode_signal_end - barcode_signal_start),
          file=sys.stderr)
    if adapter_barcode_gap < 0 or adapter_barcode_gap > 300:
        print('  verdict: skipping due to odd adapter-barcode arrangement', file=sys.stderr)
        return True
    else:
        return False


def make_non_barcoded_training_samples(adapter_seq_start, adapter_seq_end, ref_start, ref_end,
                                       adapter_signal_start, adapter_signal_end, signal,
                                       signal_size):
    print('  verdict: good no-barcode training read', file=sys.stderr)
    print('    base coords: adapter: {}-{},'
          ' ref: {}-{}'.format(adapter_seq_start, adapter_seq_end, ref_start, ref_end),
          file=sys.stderr)
    print('    signal coords: adapter: {}-{}'.format(adapter_signal_start, adapter_signal_end),
          file=sys.stderr)
    print('  making training samples', file=sys.stderr)

    for _ in range(NON_BARCODED_SAMPLES_PER_NON_BARCODED_READ):
        training_sample = get_training_sample_around_signal(signal, adapter_signal_end - 10,
                                                            adapter_signal_end + 10, signal_size,
                                                            None)
        if training_sample is not None:
            print('0\t', end='')
            print(','.join(str(s) for s in training_sample))


def make_barcoded_training_samples(barcode_name, adapter_seq_start, adapter_seq_end,
                                   barcode_start, barcode_end, ref_start, ref_end,
                                   adapter_signal_start, adapter_signal_end,
                                   barcode_signal_start, barcode_signal_end,
                                   signal, signal_size):
    print('  verdict: good training read for barcode {}'.format(barcode_name), file=sys.stderr)
    print('    base coords: adapter: {}-{}, barcode{}: {}-{}, '
          'ref: {}-{}'.format(adapter_seq_start, adapter_seq_end, barcode_name,
                              barcode_start, barcode_end, ref_start, ref_end), file=sys.stderr)
    print('    signal coords: adapter: {}-{}, '
          'barcode: {}-{}'.format(adapter_signal_start, adapter_signal_end,
                                  barcode_signal_start, barcode_signal_end), file=sys.stderr)
    print('  making training samples', file=sys.stderr)

    for _ in range(BARCODED_SAMPLES_PER_BARCODED_READ):
        training_sample = \
            get_training_sample_around_signal(signal, barcode_signal_start, barcode_signal_end,
                                              signal_size, barcode_name)
        if training_sample is not None:
            print('{}\t'.format(barcode_name), end='')
            print(','.join(str(s) for s in training_sample))

    if NON_BARCODED_SAMPLE_FROM_BEFORE_BARCODE:
        training_sample = get_training_sample_before_signal(signal, adapter_signal_end - 10,
                                                            signal_size)
        if training_sample is not None:
            print('0\t', end='')
            print(','.join(str(s) for s in training_sample))


def get_training_sample_around_signal(signal, include_start, include_end, signal_size,
                                      barcode_name):
    """
    This function takes in a large signal and returns a training-sized chunk which includes the
    specified range.
    """
    include_size = include_end - include_start
    min_start = max(0, include_start + include_size - signal_size)
    training_start = random.randint(min_start, include_start)
    training_end = training_start + signal_size

    if barcode_name is None:
        print('    no-barcode sample taken from trimmed signal: '
              '{}-{}'.format(training_start, training_end), file=sys.stderr)
    else:
        print('    barcode {} sample taken from trimmed signal: '
              '{}-{}'.format(barcode_name, training_start, training_end), file=sys.stderr)
    return signal[training_start:training_end]


def get_training_sample_before_signal(signal, before_point, signal_size):
    """
    This function takes in a large signal and returns a training-sized chunk which occurs just
    before the given point.
    """
    try:
        training_start = random.randint(0, before_point - signal_size)
    except ValueError:
        return None
    training_end = training_start + signal_size
    print('    no-barcode sample taken from trimmed signal: '
          '{}-{}'.format(training_start, training_end), file=sys.stderr)
    return signal[training_start:training_end]
