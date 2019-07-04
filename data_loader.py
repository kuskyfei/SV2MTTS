import itertools
import os
import numpy as np
import pandas as pd
from tensorflow.python.keras.utils import Sequence
from tensorflow.python.keras.preprocessing.sequence import pad_sequences

from utterance_utils import mel_for_speaker_embeddings, text_to_nparray, get_spectrograms


class RandomTrainGenerator(Sequence):
    def __init__(self, batch_size):
        self.batch_size = batch_size

    def __len__(self):
        return 50

    def __getitem__(self, index):
        return [np.random.randint(0, 32, (self.batch_size, 23)),
                np.random.rand(self.batch_size, 3, 160, 40),
                np.random.rand(self.batch_size, 33, 400)], \
               [np.random.rand(self.batch_size, 33, 400),
                np.random.rand(self.batch_size, 33 * 5, 1025)]


class SpeakerEmbeddingPredictionGenerator(Sequence):
    def __init__(self, numpied_dir,
                 batch_size,
                 sliding_window_size,
                 sample_rate,
                 n_fft,
                 hop_length,
                 win_length,
                 n_mels,
                 ref_db,
                 max_db
                 ):
        self.batch_size = batch_size
        self.sliding_window_size = sliding_window_size
        self.sample_rate = sample_rate
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.win_length = win_length
        self.n_mels = n_mels
        self.ref_db = ref_db
        self.max_db = max_db

        df = pd.read_csv(os.path.join(numpied_dir, 'trans.tsv'), header=None, sep='\t')
        df['len'] = df[2].str.len()
        df = df.sort_values('len').reset_index(drop=True)
        ids = np.array(list(df[0].str.split('_')))
        self.all_utterances = os.path.abspath(numpied_dir) + '/' + pd.Series(ids[:, 0]) + '/' + \
                              pd.Series(ids[:, 1]) + '/' + df[0] + '.npy'

    def __len__(self):
        return len(self.all_utterances) // self.batch_size + 1

    def get_all_utterances(self):
        return list(self.all_utterances)

    def __getitem__(self, index):
        current_batch = self.all_utterances[index * self.batch_size: (index + 1) * self.batch_size]
        mel_specs = [
            mel_for_speaker_embeddings(utt, sample_rate=self.sample_rate, n_fft=self.n_fft, hop_length=self.hop_length,
                                       win_length=self.win_length, n_mels=self.n_mels, ref_db=self.ref_db,
                                       max_db=self.max_db) for utt in current_batch]
        mel_slided = [np.stack(
            [utt[i: i + self.sliding_window_size] for i in range(0, utt.shape[0], int(self.sliding_window_size // 2)) if
             (i + self.sliding_window_size) <= utt.shape[0]]) for utt in mel_specs]
        # padding
        max_len = np.max([utt.shape[0] for utt in mel_slided])
        padded_mel_slides = np.stack(
            [np.pad(utt, ([0, max_len - utt.shape[0]], [0, 0], [0, 0]), mode='constant') for utt in mel_slided], axis=0)

        return padded_mel_slides


class SynthesizerTrainGenerator(Sequence):
    def __init__(self,
                 numpied_dir,
                 batch_size,
                 num_buckets,
                 output_per_step,
                 vocab,
                 sample_rate,
                 preemphasize,
                 hop_length,
                 win_length,
                 n_fft,
                 window,
                 n_mels,
                 ref_db,
                 max_db,
                 shuffle=True):
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.output_per_step = output_per_step
        self.sample_rate = sample_rate
        self.vocab = vocab
        self.preemphasize = preemphasize
        self.hop_length = hop_length
        self.win_length = win_length
        self.n_fft = n_fft
        self.window = window
        self.n_mels = n_mels
        self.ref_db = ref_db
        self.max_db = max_db

        df = pd.read_csv(os.path.join(numpied_dir, 'trans.tsv'), header=None, sep='\t',
                         names=['utt', 'original', 'normalized', 'sample_length'])
        df['len'] = df['normalized'].str.len()
        df['bin'] = pd.cut(df['sample_length'], bins=num_buckets, labels=[i for i in range(num_buckets)])
        ids = np.array(list(df['utt'].str.split('_')))
        df['utt'] = os.path.abspath(numpied_dir) + '/' + pd.Series(ids[:, 0]) + '/' + \
                    pd.Series(ids[:, 1]) + '/' + df['utt'] + '.npy'
        df['embed'] = df['utt'].str.replace('.npy', '_embed.npy')
        df['normalized'] = df['normalized'] + self.vocab[1]  # EOS

        self.buckets = [bckt.reset_index(drop=True) for _, bckt in df.groupby('bin') if len(bckt) > 0]

        if self.shuffle:
            self.buckets = [bckt.sample(frac=1).reset_index(drop=True) for bckt in self.buckets]

        self.batches = self._batches_from_buckets()

        if self.shuffle:
            np.random.shuffle(self.batches)

    def _batches_from_buckets(self):
        batches = [np.split(bckt, np.arange(self.batch_size,
                                            (len(bckt) // self.batch_size + 1) * self.batch_size,
                                            self.batch_size))
                   for bckt in self.buckets]
        batches = list(itertools.chain.from_iterable(batches))
        batches = [batch.reset_index(drop=True) for batch in batches if len(batch) > 0]
        return batches

    def __len__(self):
        return len(self.batches)

    def on_epoch_end(self):
        if self.shuffle:
            self.buckets = [bckt.sample(frac=1).reset_index(drop=True) for bckt in self.buckets]
            self.batches = self._batches_from_buckets()
            np.random.shuffle(self.batches)

    def __getitem__(self, index):
        current_batch = self.batches[index]
        utterances = [np.load(utt) for utt in current_batch['utt']]
        embeddings = np.stack([np.load(embed) for embed in current_batch['embed']], axis=0)

        text_int = pad_sequences(current_batch['normalized'].apply(text_to_nparray), padding='post', dtype='int32')

        utterances_spectrograms = [get_spectrograms(utt,
                                                    self.sample_rate,
                                                    self.preemphasize,
                                                    self.hop_length,
                                                    self.win_length,
                                                    self.n_fft,
                                                    self.window,
                                                    self.n_mels,
                                                    self.ref_db,
                                                    self.max_db) for utt in utterances]
        mag_spec, mel_spec = zip(*utterances_spectrograms)

        max_frames = np.max([mel.shape[0] for mel in mel_spec])
        max_frames = max_frames + (self.output_per_step - max_frames % self.output_per_step) \
            if max_frames % self.output_per_step != 0 else max_frames

        mag_spec = pad_sequences(mag_spec, maxlen=max_frames, dtype='float32', padding='post')
        mel_spec = pad_sequences(mel_spec, maxlen=max_frames, dtype='float32', padding='post')

        mel_spec = np.reshape(mel_spec,
                              (-1, mel_spec.shape[1] // self.output_per_step, mel_spec.shape[2] * self.output_per_step))
        decoder_inputs = np.concatenate((np.zeros_like(mel_spec[:, :1, :]), mel_spec[:, :-1, :]), 1)

        return [text_int, embeddings, decoder_inputs], [mel_spec, mag_spec]
