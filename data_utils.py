import os
import random
import torch
import torch.utils.data
import numpy as np
from mel_processing import spectrogram_torch
from utils import load_wav_to_torch, load_filepaths_and_text


class TextAudioLoader(torch.utils.data.Dataset):
    """
        1) 加载音频、HuBERT 音素对
        2) 为音频文件计算并保存时频图
    """

    def __init__(self, audiopaths_and_text, hparams):
        # 加载数据集描述文件（datasets 下面的 txt 文件）
        self.audiopaths_and_text = load_filepaths_and_text(audiopaths_and_text)
        # SoVits 实际上没有使用的东西
        self.text_cleaners = hparams.text_cleaners
        # WAV 文件中最大的波长
        self.max_wav_value = hparams.max_wav_value
        # 请参考 ./documentations/spectrogram.md
        # 音频文件采样率
        self.sampling_rate = hparams.sampling_rate
        # 用于时频谱的片段采样长度
        self.filter_length = hparams.filter_length
        # 用于时频谱的片段间距离
        self.hop_length = hparams.hop_length
        # Hann 窗长度
        self.win_length = hparams.win_length
        # 音频采样率
        self.sampling_rate = hparams.sampling_rate

        # SoVits 没有使用
        self.cleaned_text = getattr(hparams, "cleaned_text", False)

        # SoVits 没有使用
        self.add_blank = hparams.add_blank
        # 标记 HuBERT 最小语素单元数量
        self.min_text_len = getattr(hparams, "min_text_len", 1)
        # 标记 HuBERT 最大语素单元数量
        self.max_text_len = getattr(hparams, "max_text_len", 190)

        # 设置采样随机种子
        random.seed(getattr(hparams, "train_data_shuffle_seed"), 1234)
        # 打乱数据集
        random.shuffle(self.audiopaths_and_text)
        #  处理 HuBERT 音素并存储其时频谱长度
        self._filter()

    def _filter(self):
        """
        处理 HuBERT 音素并存储其时频谱长度
        """
        # Store spectrogram lengths for Bucketing
        # 为 Bucketing 过程创建时频谱长度
        # wav_length ~= file_size / (wav_channels * Bytes per dim) = file_size / (1 * 2)
        # wav 长度计算公式如上
        # spec_length = wav_length // hop_length
        # 时频谱长度计算公式如上
        lengths = []
        for audiopath, text, pitch in self.audiopaths_and_text:
            lengths.append(os.path.getsize(audiopath) // (2 * self.hop_length))
        self.lengths = lengths

    def get_audio_text_pair(self, audiopath_and_text):
        # 获取 HuBERT 因素、时频谱、原音频和 F0
        audiopath, text, pitch = audiopath_and_text[0], audiopath_and_text[1], audiopath_and_text[2]
        text = self.get_text(text)
        spec, wav = self.get_audio(audiopath)
        pitch = self.get_pitch(pitch)
        return (text, spec, wav, pitch)

    def get_pitch(self, pitch):
        # 获取 Pitch
        return torch.load(pitch)

    def get_audio(self, filename):
        # 获取音频和时频谱，如果没有找到时频谱则创建一个
        audio, sampling_rate = load_wav_to_torch(filename, normalize=False)
        if sampling_rate != self.sampling_rate:
            raise ValueError("{} {} SR doesn't match target {} SR".format(
                sampling_rate, self.sampling_rate))
        audio_norm = audio / self.max_wav_value
        audio_norm = audio_norm.unsqueeze(0)
        spec_filename = filename.replace(".wav", ".spec.pt")
        if os.path.exists(spec_filename):
            spec = torch.load(spec_filename)
        else:
            spec = spectrogram_torch(audio_norm, self.filter_length,
                                     self.sampling_rate, self.hop_length, self.win_length,
                                     center=False)
            spec = torch.squeeze(spec, 0)
            torch.save(spec, spec_filename)
        return spec, audio_norm

    def get_text(self, text):
        # 获取 HuBERT 语素
        soft = np.load(text)
        text_norm = torch.FloatTensor(soft)
        return text_norm

    def __getitem__(self, index):
        return self.get_audio_text_pair(self.audiopaths_and_text[index])

    def __len__(self):
        return len(self.audiopaths_and_text)


class TextAudioCollate:
    """ Zero-pads model inputs and targets
    """

    def __init__(self, return_ids=False):
        self.return_ids = return_ids

    def __call__(self, batch):
        """Collate's training batch from normalized text and aduio
        PARAMS
        ------
        batch: [text_normalized, spec_normalized, wav_normalized]
        """
        # Right zero-pad all one-hot text sequences to max input length
        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x[1].size(1) for x in batch]),
            dim=0, descending=True)

        max_text_len = max([len(x[0]) for x in batch])
        max_spec_len = max([x[1].size(1) for x in batch])
        max_wav_len = max([x[2].size(1) for x in batch])
        max_pitch_len = max([x[3].shape[0] for x in batch])
        # print(batch)

        text_lengths = torch.LongTensor(len(batch))
        spec_lengths = torch.LongTensor(len(batch))
        wav_lengths = torch.LongTensor(len(batch))

        text_padded = torch.FloatTensor(len(batch), max_text_len, 256)
        spec_padded = torch.FloatTensor(len(batch), batch[0][1].size(0), max_spec_len)
        wav_padded = torch.FloatTensor(len(batch), 1, max_wav_len)
        pitch_padded = torch.LongTensor(len(batch), max_pitch_len)

        text_padded.zero_()
        spec_padded.zero_()
        wav_padded.zero_()
        pitch_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            row = batch[ids_sorted_decreasing[i]]

            text = row[0]
            text_padded[i, :text.size(0), :] = text
            text_lengths[i] = text.size(0)

            spec = row[1]
            spec_padded[i, :, :spec.size(1)] = spec
            spec_lengths[i] = spec.size(1)

            wav = row[2]
            wav_padded[i, :, :wav.size(1)] = wav
            wav_lengths[i] = wav.size(1)

            pitch = row[3]
            pitch_padded[i, :pitch.size(0)] = pitch

        if self.return_ids:
            return text_padded, text_lengths, spec_padded, spec_lengths, wav_padded, wav_lengths, ids_sorted_decreasing, pitch_padded
        return text_padded, text_lengths, spec_padded, spec_lengths, wav_padded, wav_lengths, pitch_padded


"""Multi speaker version"""


class TextAudioSpeakerLoader(torch.utils.data.Dataset):
    """
        1) loads audio, speaker_id, text pairs
        2) normalizes text and converts them to sequences of integers
        3) computes spectrograms from audio files.
    """

    """
         在这里加载训练数据集
         使用配置文件中声明的 training_files 的 txt 引导文件加载数据，同时传入一些从json中读取的配置项目

         text_cleaners: 未使用
         max_wav_value: 最大波值
         sampling_rate: 训练音频采样率
         filter_length: 时频谱段大小
         hop_length: 时频谱段间距
         win_length: Hann 窗口长度
         sampling_rate: 原始音频采样率

         cleaned_text: 未使用
         add_blank: 未使用
         min_text_len: 最小HuBERT 语素单元(n)长度 [HuBERT 张量结构 (n, 256)，其中 256 为 HuBERT 用于描述单个语素的特征向量]
         max_text_len: 最大HuBERT 语素单元长度
     """

    def __init__(self, audiopaths_sid_text, hparams):
        """
            预处理并加载文本数据，该方法能够自动移除文本两边空格，文本数据结构如下：
            something-text.txt (UTF-8)
            音频路径 | 说话人ID | 文本（当然在该模型中这不是文本）| F0基频文件
        """
        self.audiopaths_sid_text = load_filepaths_and_text(audiopaths_sid_text)
        self.text_cleaners = hparams.text_cleaners
        self.max_wav_value = hparams.max_wav_value
        self.sampling_rate = hparams.sampling_rate
        self.filter_length = hparams.filter_length
        self.hop_length = hparams.hop_length
        self.win_length = hparams.win_length

        self.cleaned_text = getattr(hparams, "cleaned_text", False)

        self.add_blank = hparams.add_blank
        self.min_text_len = getattr(hparams, "min_text_len", 1)
        self.max_text_len = getattr(hparams, "max_text_len", 190)

        random.seed(getattr(hparams, "hparams.train_data_shuffle_seed", 1234))
        random.shuffle(self.audiopaths_sid_text)
        self._filter()

    def _filter(self):
        """
        为后续数据分桶过滤音素，存储其时频图长度
        Filter text & store spec lengths
        """
        # Store spectrogram lengths for Bucketing
        # wav_length ~= file_size / (wav_channels * Bytes per dim) = file_size / (1 * 2)
        # spec_length = wav_length // hop_length

        lengths = []
        for audiopath, sid, text, pitch, _ in self.audiopaths_sid_text:
            lengths.append(os.path.getsize(audiopath) // (2 * self.hop_length))
        self.lengths = lengths

    def get_audio_text_speaker_pair(self, audiopath_sid_text):
        """
        将训练集的txt描述文件转换为真正的数据
        text: HuBERT 语素
        spec: 时频图
        wav: 声音张量
        pitch: 声调张量
        sid: 说话人 ID 张量
        """
        # 分离文件名，说话人ID，文本，音调
        (audiopath,
         sid,
         text,
         pitch) = audiopath_sid_text[0], audiopath_sid_text[1], audiopath_sid_text[2], audiopath_sid_text[3]
        """
        该方法进行过改进，加载的并不是 text，实际上加载的是 HuBERT soft content encoder 处理后的多维向量
        在加载完毕后使用 torch 转为浮点向量随后返回
        """
        text = self.get_text(text)
        spec, wav = self.get_audio(audiopath)
        # sid 转 torch 向量
        sid = self.get_sid(sid)
        # 从磁盘加载音调文件
        pitch = self.get_pitch(pitch)

        return text, spec, wav, pitch, sid

    def get_audio(self, filename):
        """
        将声音波形文件加载为时频图，同时返回转换为二维向量的声音张量
        """
        audio, sampling_rate = load_wav_to_torch(filename, False)
        # 训练文件采样率和设置采样率不一样，报错
        if sampling_rate != self.sampling_rate:
            raise ValueError("{} {} SR doesn't match target {} SR".format(
                filename,
                sampling_rate,
                self.sampling_rate))
        # 正则化（规则化）
        audio_norm = audio / self.max_wav_value
        # 升维
        # [1, 2, 3, 4, 5 ... 1000] => [[1, 2, 3, 4, 5 ... 1000]]
        audio_norm = audio_norm.unsqueeze(0)
        # 修改文件名后缀
        spec_filename = filename.replace(".wav", ".spec.pt")
        # 如果存以这个后缀结尾的文件（时频图）
        if os.path.exists(spec_filename):
            # 直接加载
            spec = torch.load(spec_filename)
        else:
            # 如果不存以这个后缀结尾的文件，将音频数据转换为时频图，升维后保存起来方便以后使用
            spec = spectrogram_torch(audio_norm, self.filter_length,
                                     self.sampling_rate, self.hop_length, self.win_length,
                                     center=False)
            spec = torch.squeeze(spec, 0)
            torch.save(spec, spec_filename)
        # 返回时频图和正则化后的音频数据
        return spec, audio_norm

    def get_text(self, text):
        """
        该方法进行过改进，加载的并不是 text，实际上加载的是 HuBERT soft content encoder 处理后的多维向量
        在加载完毕后使用 torch 转为浮点向量随后返回
        """
        return torch.load(text)

    def get_pitch(self, pitch):
        """
        从磁盘中加载F0基频(音调)文件
        """
        return torch.load(pitch)

    def get_sid(self, sid):
        sid = torch.LongTensor([int(sid)])
        return sid

    """
        torch Dataset 用于加载数据的方法
        在这里，该方法首先访问要加载的原始数据，随后将数据传入加载函数请求加载
    """

    def __getitem__(self, index):
        return self.get_audio_text_speaker_pair(self.audiopaths_sid_text[index])

    """
        返回训练集长度
    """
    def __len__(self):
        return len(self.audiopaths_sid_text)


class TextAudioSpeakerCollate:
    """ Zero-pads model inputs and targets
    数据校对器
    """

    def __init__(self, return_ids=False):
        self.return_ids = return_ids

    def __call__(self, batch):
        """Collate's training batch from normalized text, audio and speaker identities
        PARAMS
        ------
        batch: [text_normalized, spec_normalized, wav_normalized, sid]
        """
        # Right zero-pad all one-hot text sequences to max input length
        _, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([x[1].size(1) for x in batch]),
            dim=0, descending=True)

        max_text_len = max([len(x[0]) for x in batch])
        max_spec_len = max([x[1].size(1) for x in batch])
        max_wav_len = max([x[2].size(1) for x in batch])
        max_pitch_len = max([x[3].shape[0] for x in batch])

        text_lengths = torch.LongTensor(len(batch))
        spec_lengths = torch.LongTensor(len(batch))
        wav_lengths = torch.LongTensor(len(batch))
        sid = torch.LongTensor(len(batch))

        text_padded = torch.FloatTensor(len(batch), max_text_len, 256)
        spec_padded = torch.FloatTensor(len(batch), batch[0][1].size(0), max_spec_len)
        wav_padded = torch.FloatTensor(len(batch), 1, max_wav_len)
        pitch_padded = torch.LongTensor(len(batch), max_pitch_len)

        text_padded.zero_()
        spec_padded.zero_()
        wav_padded.zero_()
        pitch_padded.zero_()

        for i in range(len(ids_sorted_decreasing)):
            row = batch[ids_sorted_decreasing[i]]

            text = row[0]
            text_padded[i, :text.size(0)] = text
            text_lengths[i] = text.size(0)

            spec = row[1]
            spec_padded[i, :, :spec.size(1)] = spec
            spec_lengths[i] = spec.size(1)

            wav = row[2]
            wav_padded[i, :, :wav.size(1)] = wav
            wav_lengths[i] = wav.size(1)

            pitch = row[3]
            pitch_padded[i, :pitch.size(0)] = pitch

            sid[i] = row[4]

        if self.return_ids:
            return text_padded, text_lengths, spec_padded, spec_lengths, wav_padded, wav_lengths, pitch_padded, sid, ids_sorted_decreasing
        return text_padded, text_lengths, spec_padded, spec_lengths, wav_padded, wav_lengths, pitch_padded, sid


class DistributedBucketSampler(torch.utils.data.distributed.DistributedSampler):
    """
    Maintain similar input lengths in a batch.
    Length groups are specified by boundaries.
    Ex) boundaries = [b1, b2, b3] -> any batch is included either
    {x | b1 < length(x) <=b2} or {x | b2 < length(x) <= b3}.

    It removes samples which are not included in the boundaries.
    Ex) boundaries = [b1, b2, b3] -> any x s.t. length(x) <= b1 or length(x) > b3 are discarded.
    """

    def __init__(self, dataset, batch_size, boundaries, num_replicas=None, rank=None, shuffle=True):
        """
            dataset: 数据集
            batch_size: 一批训练数据的大小
            boundaries: [32, 300, 400, 500, 600, 700, 800, 900, 1000]，时频谱边界范围
            num_replicas: 数据拷贝数量，和 GPU 数量一致
            rank: 分布式进程序号
            shuffle: 是否打乱训练集
        """
        super().__init__(dataset, num_replicas=num_replicas, rank=rank, shuffle=shuffle)
        # 从训练数据集中拷贝每个训练数据对应的时频图分片数量
        self.lengths = dataset.lengths
        self.batch_size = batch_size
        self.boundaries = boundaries

        self.buckets, self.num_samples_per_bucket = self._create_buckets()
        self.total_size = sum(self.num_samples_per_bucket)
        self.num_samples = self.total_size // self.num_replicas

    def _create_buckets(self):
        """
            数据分桶，将原本的连续特征转换为多个二元特征
            在这里，我们将原本可能连续的时频分片长度转换为下列数据范围的中的一个
            [32, 300, 400, 500, 600, 700, 800, 900, 1000]
        """
        # 依据 boundaries 的大小 - 1 来创建 buckets
        buckets = [[] for _ in range(len(self.boundaries) - 1)]
        # 遍历
        for i in range(len(self.lengths)):
            length = self.lengths[i]
            # 通过二分法查找当前长度所在的范围
            idx_bucket = self._bisect(length)
            if idx_bucket != -1:
                buckets[idx_bucket].append(i)

        # 对不存在的某一时频分片范围直接删除
        for i in range(len(buckets) - 1, 0, -1):
            if len(buckets[i]) == 0:
                buckets.pop(i)
                self.boundaries.pop(i + 1)

        """
            此时，buckets 的数据结构如下:
            [[32, 37, 39, 40, ...], ..., [901, 906]]
            这些数字是数据集中每条数据记录的序号
            与 boundaries 范围对应
        """

        num_samples_per_bucket = []
        for i in range(len(buckets)):
            len_bucket = len(buckets[i])
            total_batch_size = self.num_replicas * self.batch_size
            rem = (total_batch_size - (len_bucket % total_batch_size)) % total_batch_size
            num_samples_per_bucket.append(len_bucket + rem)
        # 返回 buckets 和 每个 bucket 的采样数目
        return buckets, num_samples_per_bucket

    def __iter__(self):
        # deterministically shuffle based on epoch
        g = torch.Generator()
        g.manual_seed(self.epoch)

        indices = []
        if self.shuffle:
            for bucket in self.buckets:
                indices.append(torch.randperm(len(bucket), generator=g).tolist())
        else:
            for bucket in self.buckets:
                indices.append(list(range(len(bucket))))

        batches = []
        for i in range(len(self.buckets)):
            bucket = self.buckets[i]
            len_bucket = len(bucket)
            if len_bucket == 0:
                continue
            ids_bucket = indices[i]
            num_samples_bucket = self.num_samples_per_bucket[i]

            # add extra samples to make it evenly divisible
            rem = num_samples_bucket - len_bucket
            ids_bucket = ids_bucket + ids_bucket * (rem // len_bucket) + ids_bucket[:(rem % len_bucket)]

            # subsample
            ids_bucket = ids_bucket[self.rank::self.num_replicas]

            # batching
            for j in range(len(ids_bucket) // self.batch_size):
                batch = [bucket[idx] for idx in ids_bucket[j * self.batch_size:(j + 1) * self.batch_size]]
                batches.append(batch)

        if self.shuffle:
            batch_ids = torch.randperm(len(batches), generator=g).tolist()
            batches = [batches[i] for i in batch_ids]
        self.batches = batches

        assert len(self.batches) * self.batch_size == self.num_samples
        return iter(self.batches)

    def _bisect(self, x, lo=0, hi=None):
        if hi is None:
            hi = len(self.boundaries) - 1

        if hi > lo:
            mid = (hi + lo) // 2
            if self.boundaries[mid] < x <= self.boundaries[mid + 1]:
                return mid
            elif x <= self.boundaries[mid]:
                return self._bisect(x, lo, mid)
            else:
                return self._bisect(x, mid + 1, hi)
        else:
            return -1

    def __len__(self):
        return self.num_samples // self.batch_size
