import re
import torch
import random
import logging
import itertools
import torch.nn as nn
import torch.optim as optim
import nlpaug.augmenter.word as naw

from typing import Dict, List, Optional, Union
from overrides import overrides
from nltk.tree import Tree
from torch.utils.data import DataLoader
from allennlp.data import DatasetReader, Instance, allennlp_collate
from allennlp.data.fields import LabelField, TextField, Field
from allennlp.data.tokenizers import Tokenizer, SpacyTokenizer, Token
from allennlp.common.file_utils import cached_path
from allennlp.common.checks import ConfigurationError
from allennlp.data.token_indexers import TokenIndexer, SingleIdTokenIndexer
from allennlp.data.vocabulary import Vocabulary
from allennlp.modules.token_embedders import Embedding
from allennlp.modules.text_field_embedders import BasicTextFieldEmbedder
from allennlp.modules.seq2seq_encoders import GruSeq2SeqEncoder
from allennlp.modules.seq2vec_encoders import GruSeq2VecEncoder
from allennlp.models.model import Model
from allennlp.nn.util import get_text_field_mask, get_token_ids_from_text_field_tensors
from allennlp.nn.util import get_lengths_from_binary_sequence_mask, move_to_device
from allennlp.modules import FeedForward
from allennlp.training import GradientDescentTrainer
from allennlp.training.metrics import CategoricalAccuracy


logger = logging.getLogger(__name__)
USE_GPU = torch.cuda.is_available()


@DatasetReader.register("sst_tokens")
class StanfordSentimentTreeBankDatasetReader(DatasetReader):
    """
    Reads tokens and their sentiment labels from the Stanford Sentiment Treebank.
    The Stanford Sentiment Treebank comes with labels
    from 0 to 4. `"5-class"` uses these labels as is. `"3-class"` converts the
    problem into one of identifying whether a sentence is negative, positive, or
    neutral sentiment. In this case, 0 and 1 are grouped as label 0 (negative sentiment),
    2 is converted to label 1 (neutral sentiment) and 3 and 4 are grouped as label 2
    (positive sentiment). `"2-class"` turns it into a binary classification problem
    between positive and negative sentiment. 0 and 1 are grouped as the label 0
    (negative sentiment), 2 (neutral) is discarded, and 3 and 4 are grouped as the label 1
    (positive sentiment).
    Expected format for each input line: a linearized tree, where nodes are labeled
    by their sentiment.
    The output of `read` is a list of `Instance` s with the fields:
        tokens : `TextField` and
        label : `LabelField`
    Registered as a `DatasetReader` with name "sst_tokens".
    # Parameters
    token_indexers : `Dict[str, TokenIndexer]`, optional (default=`{"tokens": SingleIdTokenIndexer()}`)
        We use this to define the input representation for the text.  See :class:`TokenIndexer`.
    use_subtrees : `bool`, optional, (default = `False`)
        Whether or not to use sentiment-tagged subtrees.
    granularity : `str`, optional (default = `"5-class"`)
        One of `"5-class"`, `"3-class"`, or `"2-class"`, indicating the number
        of sentiment labels to use.
    """

    def __init__(
        self,
        token_indexers: Dict[str, TokenIndexer] = None,
        tokenizer: Optional[Tokenizer] = None,
        use_subtrees: bool = False,
        granularity: str = "5-class",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._token_indexers = token_indexers or {"tokens": SingleIdTokenIndexer()}
        self._tokenizer = tokenizer or SpacyTokenizer(split_on_spaces=True)
        self._use_subtrees = use_subtrees
        allowed_granularities = ["5-class", "3-class", "2-class"]
        if granularity not in allowed_granularities:
            raise ConfigurationError(
                "granularity is {}, but expected one of: {}".format(
                    granularity, allowed_granularities
                )
            )
        self._granularity = granularity

    @overrides
    def _read(self, file_path):
        with open(cached_path(file_path), "r") as data_file:
            logger.info("Reading instances from lines in file at: %s", file_path)
            for line in data_file.readlines():
                line = line.strip("\n")
                if not line:
                    continue
                parsed_line = Tree.fromstring(line)
                if self._use_subtrees:
                    for subtree in parsed_line.subtrees():
                        instance = self.text_to_instance(subtree.leaves(), subtree.label())
                        if instance is not None:
                            yield instance
                else:
                    instance = self.text_to_instance(parsed_line.leaves(), parsed_line.label())
                    if instance is not None:
                        yield instance

    @overrides
    def text_to_instance(self, tokens: List[str], sentiment: str = None) -> Optional[Instance]:
        """
        We take `pre-tokenized` input here, because we might not have a tokenizer in this class.
        # Parameters
        tokens : `List[str]`, required.
            The tokens in a given sentence.
        sentiment : `str`, optional, (default = `None`).
            The sentiment for this sentence.
        # Returns
        An `Instance` containing the following fields:
            tokens : `TextField`
                The tokens in the sentence or phrase.
            label : `LabelField`
                The sentiment label of the sentence or phrase.
        """
        assert isinstance(
            tokens, list
        )  # If tokens is a str, nothing breaks but the results are garbage, so we check.
        if self._tokenizer is None:

            def make_token(t: Union[str, Token]):
                if isinstance(t, str):
                    return Token(t)
                elif isinstance(t, Token):
                    return t
                else:
                    raise ValueError("Tokens must be either str or Token.")

            tokens = [make_token(x) for x in tokens]
        else:
            tokens = self._tokenizer.tokenize(" ".join(tokens))
        text_field = TextField(tokens, token_indexers=self._token_indexers)
        fields: Dict[str, Field] = {"tokens": text_field}
        if sentiment is not None:
            if self._granularity == "3-class":
                if int(sentiment) < 2:
                    sentiment = "0"
                elif int(sentiment) == 2:
                    sentiment = "1"
                else:
                    sentiment = "2"
            elif self._granularity == "2-class":
                if int(sentiment) < 2:
                    sentiment = "0"
                elif int(sentiment) == 2:
                    return None
                else:
                    sentiment = "1"
            fields["label"] = LabelField(sentiment)
        return Instance(fields)

    def get_token_indexers(self):
        return self._token_indexers


class Sentence_Augmenter(object):
    def __init__(
        self,
        vocab: Vocabulary,
        namespace: str = "tokens"
    ):
        self.vocab = vocab
        self.namespace = namespace
        self.augmenters = [
            # naw.ContextualWordEmbsAug(model_path="distilbert-base-uncased", action="substitute"),
            # naw.ContextualWordEmbsAug(model_path="roberta-base", action="substitute"),
            # naw.ContextualWordEmbsAug(model_path="albert-base-v2", action="substitute"),
            naw.SynonymAug(aug_src="wordnet"),
            naw.SynonymAug(aug_src="ppdb", model_path="pretrained_weight/ppdb-2.0-m-all"),
            # naw.RandomWordAug(action="swap")
        ]
        self.tokenizer = SpacyTokenizer()

    def _get_batch_sentences_with_token(
        self,
        batch_sentences_with_token_id: List[List[int]],
        batch_sentences_length: List[int]
    ):
        batch_sentences = []

        for sentence, sentence_len in zip(batch_sentences_with_token_id, batch_sentences_length):
            tokens = []

            for token_id in sentence[:sentence_len]:
                token = self.vocab.get_token_from_index(token_id, self.namespace)
                tokens.append(token)

            batch_sentences.append(' '.join(tokens))

        return batch_sentences

    def _augment_batch_sentences(
        self,
        action_ids: List[int],
        batch_sentences: List[str]
    ):
        augmented_batch_sentences = []

        for action_id, batch_sentence in zip(action_ids, batch_sentences):
            augmented_batch_sentence = self.augmenters[action_id].augment(batch_sentence)
            augmented_batch_sentence = re.sub(" +", " ", augmented_batch_sentence)
            augmented_batch_sentences.append(augmented_batch_sentence)

        return augmented_batch_sentences

    def get_num_of_augment_action(self):
        return len(self.augmenters)

    def _get_batch_sentences_with_token_id(
        self,
        batch_sentences: List[str]
    ) -> List[torch.Tensor]:
        batch_sentences_with_token_id = []

        for sentence in batch_sentences:
            tokens = self.tokenizer.tokenize(sentence)
            token_ids = []

            for token in tokens:
                token_id = self.vocab.get_token_index(token.text)
                token_ids.append(token_id)

            batch_sentences_with_token_id.append(torch.tensor(token_ids))

        return batch_sentences_with_token_id

    def augment_batch_sentences(
        self,
        action_ids: List[int],
        batch_sentences_dict: Dict[str, Dict[str, torch.Tensor]]
    ):
        # Get sentence list where the shape is [#_of_batch, #_of_word]
        batch_sentences_length = get_lengths_from_binary_sequence_mask(
            get_text_field_mask(batch_sentences_dict)
        ).cpu().tolist()
        batch_sentences_with_token_id = get_token_ids_from_text_field_tensors(batch_sentences_dict).cpu().tolist()

        # Get sentence list with original token instead of token id
        batch_sentences = self._get_batch_sentences_with_token(batch_sentences_with_token_id, batch_sentences_length)

        # Get Augmented Sentence
        augmented_batch_sentences = self._augment_batch_sentences(action_ids, batch_sentences)

        # Get augmented batch sentences with token id
        batch_augmented_sentences_with_token_id = self._get_batch_sentences_with_token_id(augmented_batch_sentences)

        # Padding back
        batch_augmented_sentences_with_token_id = torch.nn.utils.rnn.pad_sequence(
            batch_augmented_sentences_with_token_id, batch_first=True
        )

        return batch_augmented_sentences_with_token_id


class REINFORCE_Model():
    def __init__(
        self,
        num_of_action: int,
        s2s_encoder: GruSeq2SeqEncoder,
        s2v_encoder: GruSeq2VecEncoder
    ):
        super().__init__()
        self.num_of_action = num_of_action
        self.action_list = list(range(0, num_of_action))
        self.s2s_encoder = s2s_encoder
        self.s2v_encoder = s2v_encoder

    def forward(
        self,
        batch_sentences_dict: Dict[str, Dict[str, torch.Tensor]]
    ):
        num_of_batch_action = batch_sentences_dict["tokens"]["tokens"].shape[0]

        selected_action_list = random.choices(self.action_list, k=num_of_batch_action)

        return selected_action_list


@Model.register("sentiment_classifier")
class Sentiment_Model(Model):
    def __init__(
        self,
        vocab: Vocabulary,
        embedder: BasicTextFieldEmbedder,
        s2s_encoder: GruSeq2SeqEncoder,
        s2v_encoder: GruSeq2VecEncoder,
        feedforward: FeedForward
    ):
        super().__init__(vocab)
        # Model Augmenter
        self.vocab = vocab
        self.augmenter = Sentence_Augmenter(vocab)

        # Model Reinforce
        self.reinforce = REINFORCE_Model(self.augmenter.get_num_of_augment_action(), s2s_encoder, s2v_encoder)

        # Model Encoder Structure
        self.embedder = embedder
        self.s2s_encoder = s2s_encoder
        self.s2v_encoder = s2v_encoder

        # Model Output Structure
        self.feedforward = feedforward
        self.classification_layer = nn.Linear(self.feedforward.get_output_dim(), vocab.get_vocab_size("labels"))
        self.final_activation = nn.Softmax()

        # Loss initiailization
        self.criterion = nn.CrossEntropyLoss()
        self.accuracy = CategoricalAccuracy()

    def _anchor_forward(
        self,
        tokens,
        label
    ):
        # Embedder first
        E = self.embedder(tokens)

        # Word s2s, s2v encoding
        tokens_mask = get_text_field_mask(tokens)

        E_S = self.s2s_encoder(E, tokens_mask)
        E_V = self.s2v_encoder(E_S, tokens_mask)

        # Prepare to output
        F_E = self.feedforward(E_V)
        Z = self.classification_layer(F_E)
        A = self.final_activation(Z)

        # Prepare to model output
        output_dict = {}
        predicts = torch.argmax(A, dim=1).cpu().tolist()
        name_of_predicts = [self.vocab.get_token_from_index(predict, namespace="labels") for predict in predicts]

        output_dict = {
            "logits": Z,
            "class_probabilities": A,
            "predict_label": name_of_predicts
        }

        self.accuracy(Z, label)

        if label is not None:
            loss = self.criterion(Z, label)
            output_dict["loss"] = loss

        return output_dict

    def _augment_forward(
        self,
        augmented_tokens,
        label,
        output_dict
    ):
        # Embedder first
        E = self.embedder(augmented_tokens)

        # Word s2s, s2v encoding
        tokens_mask = get_text_field_mask(augmented_tokens)

        E_S = self.s2s_encoder(E, tokens_mask)
        E_V = self.s2v_encoder(E_S, tokens_mask)

        # Prepare to output
        F_E = self.feedforward(E_V)
        Z = self.classification_layer(F_E)

        if label is not None:
            loss = self.criterion(Z, label)
            output_dict["loss"] += loss

        return output_dict

    @overrides
    def forward(
        self,
        tokens,
        label
    ):
        # Augment sentence
        actions = self.reinforce.forward(tokens)
        augmented_tokens = {"tokens": {
            "tokens": move_to_device(self.augmenter.augment_batch_sentences(actions, tokens), 0)
        }}

        # Anchor forwarding
        output_dict = self._anchor_forward(tokens, label)
        output_dict = self._augment_forward(augmented_tokens, label, output_dict)

        return output_dict

    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        return {'accuracy': self.accuracy.get_metric(reset)}


def get_sst_ds(
    train_data_path="data/sst/train.txt",
    valid_data_path="data/sst/dev.txt",
    test_data_path="data/sst/test.txt"
):
    sst_dataset_reader = StanfordSentimentTreeBankDatasetReader()
    train_ds = sst_dataset_reader.read(train_data_path)
    valid_ds = sst_dataset_reader.read(valid_data_path)
    test_ds = sst_dataset_reader.read(test_data_path)
    token_indexers = sst_dataset_reader.get_token_indexers()

    return train_ds, valid_ds, test_ds, token_indexers


def main():
    # Get Dataset
    train_ds, valid_ds, test_ds, token_indexers = get_sst_ds()

    # Set Vocabulary Set
    vocab = Vocabulary.from_instances(train_ds)
    train_ds.index_with(vocab)
    valid_ds.index_with(vocab)
    test_ds.index_with(vocab)

    # Batch begin
    train_data_loader = DataLoader(train_ds, batch_size=200, shuffle=True, collate_fn=allennlp_collate)
    valid_data_loader = DataLoader(valid_ds, batch_size=200, collate_fn=allennlp_collate)
    test_data_loader = DataLoader(test_ds, batch_size=200, collate_fn=allennlp_collate)

    # Embedder declartion
    glove_embedding = Embedding(
        embedding_dim=200,
        vocab=vocab,
        padding_index=0,
        pretrained_file="pretrained_weight/glove.6B.200d.txt"
    )
    embedder = BasicTextFieldEmbedder(token_embedders={"tokens": glove_embedding})

    # Encoder declartion
    s2s_encoder = GruSeq2SeqEncoder(
        input_size=embedder.get_output_dim(),
        hidden_size=300,
        num_layers=2,
        bidirectional=True
    )
    s2v_encoder = GruSeq2VecEncoder(
        input_size=s2s_encoder.get_output_dim(),
        hidden_size=300,
        num_layers=2,
        bidirectional=True
    )

    # FeedForward declartion
    feedforward = FeedForward(
        input_dim=s2v_encoder.get_output_dim(),
        num_layers=2,
        hidden_dims=[300, 150],
        activations=nn.ReLU(),
        dropout=0.3
    )

    # Model declartion
    gru_sentiment_model = Sentiment_Model(
        vocab,
        embedder,
        s2s_encoder,
        s2v_encoder,
        feedforward
    )

    # Model move to gpu
    if USE_GPU is True:
        gru_sentiment_model = gru_sentiment_model.cuda()

    # Trainer Declarition
    trainer = GradientDescentTrainer(
        gru_sentiment_model,
        optim.Adam(gru_sentiment_model.parameters(), lr=0.00001),
        train_data_loader,
        validation_data_loader=valid_data_loader,
        num_epochs=500
    )

    # Trainer Train
    trainer.train()


if __name__ == '__main__':
    main()
