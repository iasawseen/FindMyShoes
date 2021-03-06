#!/usr/bin/env python3

import pathlib
import collections
import argparse

import json
import struct

import sys
import mmap

from collections import namedtuple, Counter

from text_utils import TextExtractor

# Store entries in inverted index as integers.
INVERTED_ENTRY_SIZE = struct.calcsize('i')

# should stay hashable for searcher.
# TODO remove those entries contained in database.
DocumentEntry = namedtuple('DocumentEntry', ['path', 'url', 'length', 'idx', 'sizes', 'price', 'name', 'image'])

class IndexBuilder:
    def __init__(self, documents):
        self.dictionary = None
        self.documents_paths = documents

    @staticmethod
    def _get_words_from_json(json_data, normalize=True):
        # add name?
        attributes = json_data.get('attributes')
        if attributes is not None:
            attributes_text = ",".join("{}:{}".format(name, value) for (name, value) in attributes.items())
        else:
            attributes_text = ""
        product_description = '\n'.join([
            json_data.get('name', ''),
            json_data.get('description', ''),
            '\n'.join(json_data.get('reviews', '')),
            attributes_text
        ])
        if normalize:
            return TextExtractor.get_normal_words_from_text(product_description)
        else:
            return TextExtractor.get_words_from_text(product_description)

    @staticmethod
    def _get_words_from_path(doc_path):
        with doc_path.open() as f:
            json_data = json.load(f)
        return IndexBuilder._get_words_from_json(json_data)

    def make_first_pass(self, dict_save_path):
        """First pass of indexation: count word occurencies and required index size.

        Computes dictionary of the following form:
        {
            documents: [json_name_of_doc1, json_name_of_doc2, ..., json_name_of_docn]
            words: {
                'азбука': {
                    'global_count': 12345       --- total number of occurencies in corpus
                    'df': 67                    --- document frequency
                    'offset_inverted': 98       --- offset (in bytes) of word's inverted index in global inverted index
                    'offset_weight': 1023       --- same as above, but in global weights index
                }
                'машина': { ... }
                ...
            }
        }
        """
        result_dict = dict()
        documents = []

        words_dict = dict()
        result_dict['words'] = words_dict
        avgdl = 0.0  # average document length

        for i, doc in enumerate(self.documents_paths):
            print('Reading document {}'.format(i))
            with doc.open() as f:
                json_data = json.load(f)
            words = self._get_words_from_json(json_data)
            avgdl += len(words)

            added_df_words = set()
            for word in words:
                if word not in words_dict:
                    words_dict[word] = dict(global_count=0, df=0, offset_inverted=0, offset_weight=0)

                words_dict[word]['global_count'] += 1
                if word not in added_df_words:
                    words_dict[word]['df'] += 1
                    added_df_words.add(word)

            url = json_data.get('url')
            shoe_name = json_data.get('name')
            shoe_price = json_data.get('price')
            shoe_sizes = json_data.get('sizes')
            shoe_image = json_data.get('image')
            if isinstance(shoe_sizes, list):
                shoe_sizes = tuple(shoe_sizes)
            doc_entry = DocumentEntry(path=doc.name,
                                      url=url,
                                      length=len(words),
                                      price=shoe_price,
                                      sizes=shoe_sizes,
                                      name=shoe_name,
                                      image=shoe_image,
                                      idx=i)
            documents.append(doc_entry)
        result_dict['documents'] = list(map(lambda doc: doc._asdict(), documents))
        avgdl /= len(self.documents_paths)
        result_dict['avgdl'] = avgdl
        print('Scanned all documents')

        current_offset = 0
        for word in words_dict:
            words_dict[word]['offset_inverted'] = current_offset
            words_dict[word]['offset_weight'] = current_offset
            current_offset += INVERTED_ENTRY_SIZE * words_dict[word]['global_count']

        self.dictionary = result_dict
        with open(dict_save_path, 'w') as dict_out:
            json.dump(self.dictionary, dict_out, ensure_ascii=False)

        print('First pass finished')

    def _make_second_pass_inner(self, weights, inverted):
        current_write_position = dict()
        for word in self.dictionary['words']:
            current_write_position[word] = self.dictionary['words'][word]['offset_inverted']

        for i, doc in enumerate(self.documents_paths):
            words = self._get_words_from_path(doc)
            tf = Counter(words)

            # Write document id in inverted list, term frequency to weights list.
            for word in set(words):
                cur_offs = current_write_position[word]

                weights[cur_offs:cur_offs + INVERTED_ENTRY_SIZE] = struct.pack('i', tf[word])
                inverted[cur_offs:cur_offs + INVERTED_ENTRY_SIZE] = struct.pack('i', i)

                current_write_position[word] += INVERTED_ENTRY_SIZE

        print('Second pass finished')

    def make_second_pass(self, inverted_index_path, weight_path):
        """Second pass of indexation: actually store data to index.
        """
        word_freq_pairs = [(word, self.dictionary['words'][word]['global_count'])
                           for word in self.dictionary['words']]
        word_freq_pairs = sorted(word_freq_pairs, key=lambda x: x[1], reverse=True)
        index_size = sum(map(lambda x: x[1], word_freq_pairs)) * INVERTED_ENTRY_SIZE

        print('Required index size {} bytes'.format(index_size))
        print('Top 30 words: {}'.format(word_freq_pairs[:30]))

        with open(inverted_index_path, 'w+b') as inverted_index:
            with open(weight_path, 'w+b') as weight_index:
                inverted_index.write(b'\0' * index_size)
                weight_index.write(b'\0' * index_size)
                inverted_index.flush()
                weight_index.flush()
                with mmap.mmap(weight_index.fileno(), index_size) as weight_mmap:
                    with mmap.mmap(inverted_index.fileno(), index_size) as inverted_mmap:
                        self._make_second_pass_inner(weight_mmap, inverted_mmap)


def main():
    from os.path import join
    from common import default_index_dir, default_json_dir

    parser = argparse.ArgumentParser(description="Index builder.")
    parser.add_argument("--json-dir", type=str, default=default_json_dir())
    parser.add_argument("--index-dir", type=str, default=default_index_dir())

    # TODO distinguish between input and output arguments:
    # add parsing group, for instance.
    parser.add_argument("-i", "--inverted-index-path", type=str, default=join(default_index_dir(), 'inverted.bin'))
    parser.add_argument("-w", "--weight-path", type=str, default=join(default_index_dir(), 'weight.bin'))
    parser.add_argument("-d", "--dictionary", type=str, default=join(default_index_dir(), 'dictionary.txt'))

    args = parser.parse_args()

    documents = list(pathlib.Path(args.json_dir).glob('*.json'))
    index_builder = IndexBuilder(documents)
    index_builder.make_first_pass(args.dictionary)
    index_builder.make_second_pass(inverted_index_path=args.inverted_index_path, weight_path=args.weight_path)

    return 0


if __name__ == '__main__':
    sys.exit(main())
