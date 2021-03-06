import sys
import re
from time import time
from os.path import isfile
from parameters import *
from collections import defaultdict

def normalize(x):
    # x = re.sub("[\uAC00-\uD7A3]+", "\uAC00", x) £ convert Hangeul to 가
    # x = re.sub("[\u3040-\u30FF]+", "\u3042", x) # convert Hiragana and Katakana to あ
    # x = re.sub("[\u4E00-\u9FFF]+", "\u6F22", x) # convert CJK unified ideographs to 漢
    x = re.sub("\s+", " ", x)
    x = re.sub("^ | $", "", x)
    x = x.lower()
    return x

def tokenize(x, norm = True):
    if norm:
        x = normalize(x)
    if UNIT == "char":
        return re.sub(" ", "", x)
    if UNIT in ("word", "sent"):
        return x.split(" ")

def save_data(filename, data):
    fo = open(filename, "w")
    for seq in data:
        fo.write((" ".join(seq[0]) + "\t" + " ".join(seq[1]) if seq else "") + "\n")
    fo.close()

def load_tkn_to_idx(filename):
    print("loading %s" % filename)
    tkn_to_idx = {}
    fo = open(filename)
    for line in fo:
        line = line[:-1]
        tkn_to_idx[line] = len(tkn_to_idx)
    fo.close()
    return tkn_to_idx

def load_idx_to_tkn(filename):
    print("loading %s" % filename)
    idx_to_tkn = []
    fo = open(filename)
    for line in fo:
        line = line[:-1]
        idx_to_tkn.append(line)
    fo.close()
    return idx_to_tkn

def save_tkn_to_idx(filename, tkn_to_idx):
    fo = open(filename, "w")
    for tkn, _ in sorted(tkn_to_idx.items(), key = lambda x: x[1]):
        fo.write("%s\n" % tkn)
    fo.close()

def load_checkpoint(filename, model = None):
    print("loading %s" % filename)
    checkpoint = torch.load(filename)
    if model:
        model.load_state_dict(checkpoint["state_dict"])
    epoch = checkpoint["epoch"]
    loss = checkpoint["loss"]
    print("saved model: epoch = %d, loss = %f" % (checkpoint["epoch"], checkpoint["loss"]))
    return epoch

def save_checkpoint(filename, model, epoch, loss, time):
    print("epoch = %d, loss = %f, time = %f" % (epoch, loss, time))
    if filename and model:
        print("saving %s" % filename)
        checkpoint = {}
        checkpoint["state_dict"] = model.state_dict()
        checkpoint["epoch"] = epoch
        checkpoint["loss"] = loss
        torch.save(checkpoint, filename + ".epoch%d" % epoch)
        print("saved model at epoch %d" % epoch)

class data():
    def __init__(self):
        self.idx = None # input index
        self.x0 = [[]] # raw input
        self.x1 = [[]] # tokenized input
        self.xc = [[]] # indexed input, character-level
        self.xw = [[]] # indexed input, word-level
        self.y0 = [[]] # actual output
        self.y1 = [] # predicted output
        self.lens = None # document lengths
        self.prob = [] # probability
        self.attn = [] # attention heatmap

    def sort(self):
        self.idx = list(range(len(self.x0)))
        self.idx.sort(key = lambda x: -len(self.xw[x]))
        if self.x0: self.x0 = [self.x0[i] for i in self.idx]
        if self.x1: self.x1 = [self.x1[i] for i in self.idx]
        self.xc = [self.xc[i] for i in self.idx]
        self.xw = [self.xw[i] for i in self.idx]

    def unsort(self):
        self.idx = sorted(range(len(self.x0)), key = lambda x: self.idx[x])
        if self.x0: self.x0 = [self.x0[i] for i in self.idx]
        if self.x1: self.x1 = [self.x1[i] for i in self.idx]
        self.xc = [self.xc[i] for i in self.idx]
        self.xw = [self.xw[i] for i in self.idx]
        self.y1 = [self.y1[i] for i in self.idx]
        if self.prob:
            self.prob = [self.prob[i] for i in self.idx]
        if self.attn:
            self.attn = [self.attn[i] for i in self.idx]

class dataloader():
    def __init__(self):
        for a, b in data().__dict__.items():
            setattr(self, a, b)

    def append_item(self, x0 = None, x1 = None, xc = None, xw = None, y0 = None):
        if x0: self.x0[-1].append(x0)
        if x1: self.x1[-1].append(x1)
        if xc: self.xc[-1].append(xc)
        if xw: self.xw[-1].append(xw)
        if y0: self.y0[-1].extend(y0)

    def append_row(self):
        self.x0.append([])
        self.x1.append([])
        self.xc.append([])
        self.xw.append([])
        self.y0.append([])

    def strip(self):
        if len(self.xw[-1]):
            return
        self.x0.pop()
        self.x1.pop()
        self.xc.pop()
        self.xw.pop()
        self.y0.pop()

    def split(self): # split into batches
        for i in range(0, len(self.y0), BATCH_SIZE):
            batch = data()
            j = i + min(BATCH_SIZE, len(self.x0) - i)
            batch.x0 = self.x0[i:j]
            batch.y0 = self.y0[i:j]
            batch.y1 = [[] for _ in range(j - i)]
            batch.lens = [len(x) for x in self.xw[i:j]]
            batch.prob = [Tensor([0]) for _ in range(j - i)]
            batch.attn = [[] for _ in range(j - i)]
            if HRE:
                batch.x1 = [list(x) for x in self.x1[i:j] for x in x]
                batch.xc = [list(x) for x in self.xc[i:j] for x in x]
                batch.xw = [list(x) for x in self.xw[i:j] for x in x]
            else:
                batch.x1 = [list(*x) for x in self.x1[i:j]]
                batch.xc = [list(*x) for x in self.xc[i:j]]
                batch.xw = [list(*x) for x in self.xw[i:j]]
            yield batch

    def tensor(self, bc, bw, lens = None, sos = False, eos = False):
        _p, _s, _e = [PAD_IDX], [SOS_IDX], [EOS_IDX]
        if HRE and lens:
            d_len = max(lens) # document length (Ld)
            i, _bc, _bw = 0, [], []
            for j in lens:
                if sos:
                    _bc.append([[]])
                    _bw.append([])
                _bc.extend(bc[i:i + j] + [[[]] for _ in range(d_len - j)])
                _bw.extend(bw[i:i + j] + [[] for _ in range(d_len - j)])
                if eos:
                    _bc.append([[]])
                    _bw.append([])
                i += j
            bc, bw = _bc, _bw
        if bw:
            s_len = max(map(len, bw)) # sentence length (Ls)
            bw = [_s * sos + x + _e * eos + _p * (s_len - len(x)) for x in bw]
            bw = LongTensor(bw) # [B * Ld, Ls]
        if bc:
            w_len = max(max(map(len, x)) for x in bc) # word length (Lw)
            w_pad = [_p * (w_len + 2)]
            bc = [[_s + w + _e + _p * (w_len - len(w)) for w in x] for x in bc]
            bc = [w_pad * sos + x + w_pad * (s_len - len(x) + eos) for x in bc]
            bc = LongTensor(bc) # [B * Ld, Ls, Lw]
        return bc, bw

def log_sum_exp(x):
    m = torch.max(x, -1)[0]
    return m + torch.log(torch.sum(torch.exp(x - m.unsqueeze(-1)), -1))

def iob_to_txt(x, y): # for word/sentence segmentation
    out = [[]]
    if re.match("(\S+/\S+( |$))+", x): # token/tag
        x = re.sub(r"/[^ /]+\b", "", x) # remove tags
    for i, (j, k) in enumerate(zip(tokenize(x, False), y)):
        if i and k[0] == "B":
            out.append([])
        out[-1].append(j)
    if TASK == "word-segmentation":
        d1, d2 = "", " "
    if TASK == "sentence-segmentation":
        d1, d2 = " ", "\n"
    return d2.join(d1.join(x) for x in out)

def f1(p, r):
    return 2 * p * r / (p + r) if p + r else 0
