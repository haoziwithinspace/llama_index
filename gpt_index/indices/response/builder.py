"""Response builder class.

This class provides general functions for taking in a set of text
and generating a response.

Will support different modes, from 1) stuffing chunks into prompt,
2) create and refine separately over each chunk, 3) tree summarization.

"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Generator, List, Optional, Tuple, Union, cast

from gpt_index.data_structs.data_structs import Node
from gpt_index.indices.common.tree.base import GPTTreeIndexBuilder
from gpt_index.indices.prompt_helper import PromptHelper
from gpt_index.indices.utils import get_sorted_node_list, truncate_text
from gpt_index.langchain_helpers.chain_wrapper import LLMPredictor
from gpt_index.prompts.prompts import QuestionAnswerPrompt, RefinePrompt, SummaryPrompt
from gpt_index.response.schema import SourceNode
from gpt_index.response.utils import get_response_text
from gpt_index.utils import temp_set_attrs

RESPONSE_TEXT_TYPE = Union[str, Generator]


class ResponseMode(str, Enum):
    """Response modes."""

    DEFAULT = "default"
    COMPACT = "compact"
    TREE_SUMMARIZE = "tree_summarize"
    NO_TEXT = "no_text"


@dataclass
class TextChunk:
    """Response chunk."""

    text: str
    # Whether this chunk is already a response
    is_answer: bool = False


class ResponseBuilder:
    """Response builder class."""

    def __init__(
        self,
        prompt_helper: PromptHelper,
        llm_predictor: LLMPredictor,
        text_qa_template: QuestionAnswerPrompt,
        refine_template: RefinePrompt,
        texts: Optional[List[TextChunk]] = None,
        nodes: Optional[List[Node]] = None,
        use_async: bool = False,
        streaming: bool = False,
    ) -> None:
        """Init params."""
        self.prompt_helper = prompt_helper
        self.llm_predictor = llm_predictor
        self.text_qa_template = text_qa_template
        self.refine_template = refine_template
        self._texts = texts or []
        nodes = nodes or []
        self.source_nodes: List[SourceNode] = SourceNode.from_nodes(nodes)
        self._use_async = use_async
        self._streaming = streaming

    def add_text_chunks(self, text_chunks: List[TextChunk]) -> None:
        """Add text chunk."""
        self._texts.extend(text_chunks)

    def reset(self) -> None:
        """Clear text chunks."""
        self._texts = []
        self.source_nodes = []

    def add_node_as_source(
        self, node: Node, similarity: Optional[float] = None
    ) -> None:
        """Add node."""
        self.source_nodes.append(SourceNode.from_node(node, similarity=similarity))

    def add_source_node(self, source_node: SourceNode) -> None:
        """Add source node directly."""
        self.source_nodes.append(source_node)

    def get_sources(self) -> List[SourceNode]:
        """Get sources."""
        return self.source_nodes

    def refine_response_single(
        self,
        response: RESPONSE_TEXT_TYPE,
        query_str: str,
        text_chunk: str,
    ) -> RESPONSE_TEXT_TYPE:
        """Refine response."""
        # TODO: consolidate with logic in response/schema.py
        if isinstance(response, Generator):
            response = get_response_text(response)

        fmt_text_chunk = truncate_text(text_chunk, 50)
        logging.debug(f"> Refine context: {fmt_text_chunk}")
        # NOTE: partial format refine template with query_str and existing_answer here
        refine_template = self.refine_template.partial_format(
            query_str=query_str, existing_answer=response
        )
        refine_text_splitter = self.prompt_helper.get_text_splitter_given_prompt(
            refine_template, 1
        )
        text_chunks = refine_text_splitter.split_text(text_chunk)
        for cur_text_chunk in text_chunks:
            if not self._streaming:
                response, _ = self.llm_predictor.predict(
                    refine_template,
                    context_msg=cur_text_chunk,
                )
            else:
                response, _ = self.llm_predictor.stream(
                    refine_template,
                    context_msg=cur_text_chunk,
                )
            logging.debug(f"> Refined response: {response}")
        return response

    def give_response_single(
        self,
        query_str: str,
        text_chunk: str,
    ) -> RESPONSE_TEXT_TYPE:
        """Give response given a query and a corresponding text chunk."""
        text_qa_template = self.text_qa_template.partial_format(query_str=query_str)
        qa_text_splitter = self.prompt_helper.get_text_splitter_given_prompt(
            text_qa_template, 1
        )
        text_chunks = qa_text_splitter.split_text(text_chunk)
        response: Optional[RESPONSE_TEXT_TYPE] = None
        # TODO: consolidate with loop in get_response_default
        for cur_text_chunk in text_chunks:
            if response is None and not self._streaming:
                response, _ = self.llm_predictor.predict(
                    text_qa_template,
                    context_str=cur_text_chunk,
                )
                logging.debug(f"> Initial response: {response}")
            elif response is None and self._streaming:
                response, _ = self.llm_predictor.stream(
                    text_qa_template,
                    context_str=cur_text_chunk,
                )
            else:
                response = self.refine_response_single(
                    cast(RESPONSE_TEXT_TYPE, response),
                    query_str,
                    cur_text_chunk,
                )
        if isinstance(response, str):
            response = response or "Empty Response"
        else:
            response = cast(Generator, response)
        return response

    def get_response_over_chunks(
        self,
        query_str: str,
        text_chunks: List[TextChunk],
        prev_response: Optional[str] = None,
    ) -> RESPONSE_TEXT_TYPE:
        """Give response over chunks."""
        prev_response_obj = cast(Optional[RESPONSE_TEXT_TYPE], prev_response)
        response: Optional[RESPONSE_TEXT_TYPE] = None
        for text_chunk in text_chunks:
            if prev_response_obj is None:
                # if this is the first chunk, and text chunk already
                # is an answer, then return it
                if text_chunk.is_answer:
                    response = text_chunk.text
                # otherwise give response
                else:
                    response = self.give_response_single(
                        query_str,
                        text_chunk.text,
                    )
            else:
                response = self.refine_response_single(
                    prev_response_obj, query_str, text_chunk.text
                )
            prev_response_obj = response
        if isinstance(response, str):
            response = response or "Empty Response"
        else:
            response = cast(Generator, response)
        return response

    def _get_response_default(
        self, query_str: str, prev_response: Optional[str]
    ) -> RESPONSE_TEXT_TYPE:
        return self.get_response_over_chunks(
            query_str, self._texts, prev_response=prev_response
        )

    def _get_response_compact(
        self, query_str: str, prev_response: Optional[str]
    ) -> RESPONSE_TEXT_TYPE:
        """Get compact response."""
        # use prompt helper to fix compact text_chunks under the prompt limitation
        max_prompt = self.prompt_helper.get_biggest_prompt(
            [self.text_qa_template, self.refine_template]
        )
        with temp_set_attrs(self.prompt_helper, use_chunk_size_limit=False):
            new_texts = self.prompt_helper.compact_text_chunks(
                max_prompt, [t.text for t in self._texts]
            )
            new_text_chunks = [TextChunk(text=t) for t in new_texts]
            response = self.get_response_over_chunks(
                query_str, new_text_chunks, prev_response=prev_response
            )
        return response

    def _get_tree_index_builder_and_nodes(
        self,
        summary_template: SummaryPrompt,
        query_str: str,
        num_children: int = 10,
    ) -> Tuple[GPTTreeIndexBuilder, Dict]:
        """Get tree index builder."""
        # first join all the text chunks into a single text
        all_text = "\n\n".join([t.text for t in self._texts])
        # then get text splitter
        text_splitter = self.prompt_helper.get_text_splitter_given_prompt(
            summary_template, num_children
        )
        text_chunks = text_splitter.split_text(all_text)
        all_nodes: Dict[int, Node] = {
            i: Node(text=t) for i, t in enumerate(text_chunks)
        }

        index_builder = GPTTreeIndexBuilder(
            num_children,
            summary_template,
            self.llm_predictor,
            self.prompt_helper,
            text_splitter,
            use_async=self._use_async,
        )
        return index_builder, all_nodes

    def _get_tree_response_over_root_nodes(
        self,
        query_str: str,
        prev_response: Optional[str],
        root_nodes: Dict[int, Node],
        text_qa_template: QuestionAnswerPrompt,
    ) -> RESPONSE_TEXT_TYPE:
        """Get response from tree builder over root nodes."""
        node_list = get_sorted_node_list(root_nodes)
        node_text = self.prompt_helper.get_text_from_nodes(
            node_list, prompt=text_qa_template
        )
        # NOTE: the final response could be a string or a stream
        response = self.get_response_over_chunks(
            query_str,
            [TextChunk(node_text)],
            prev_response=prev_response,
        )
        if isinstance(response, str):
            response = response or "Empty Response"
        return response

    def _get_response_tree_summarize(
        self,
        query_str: str,
        prev_response: Optional[str],
        num_children: int = 10,
    ) -> RESPONSE_TEXT_TYPE:
        """Get tree summarize response."""
        text_qa_template = self.text_qa_template.partial_format(query_str=query_str)
        summary_template = SummaryPrompt.from_prompt(text_qa_template)

        index_builder, all_nodes = self._get_tree_index_builder_and_nodes(
            summary_template, query_str, num_children
        )
        root_nodes = index_builder.build_index_from_nodes(all_nodes, all_nodes)
        return self._get_tree_response_over_root_nodes(
            query_str, prev_response, root_nodes, text_qa_template
        )

    async def _aget_response_tree_summarize(
        self,
        query_str: str,
        prev_response: Optional[str],
        num_children: int = 10,
    ) -> RESPONSE_TEXT_TYPE:
        """Get tree summarize response."""
        text_qa_template = self.text_qa_template.partial_format(query_str=query_str)
        summary_template = SummaryPrompt.from_prompt(text_qa_template)

        index_builder, all_nodes = self._get_tree_index_builder_and_nodes(
            summary_template, query_str, num_children
        )
        root_nodes = await index_builder.abuild_index_from_nodes(all_nodes, all_nodes)
        return self._get_tree_response_over_root_nodes(
            query_str, prev_response, root_nodes, text_qa_template
        )

    def get_response(
        self,
        query_str: str,
        prev_response: Optional[str] = None,
        mode: ResponseMode = ResponseMode.DEFAULT,
        **response_kwargs: Any,
    ) -> RESPONSE_TEXT_TYPE:
        """Get response."""
        if mode == ResponseMode.DEFAULT:
            return self._get_response_default(query_str, prev_response)
        elif mode == ResponseMode.COMPACT:
            return self._get_response_compact(query_str, prev_response)
        elif mode == ResponseMode.TREE_SUMMARIZE:
            return self._get_response_tree_summarize(
                query_str, prev_response, **response_kwargs
            )
        else:
            raise ValueError(f"Invalid mode: {mode}")

    async def aget_response(
        self,
        query_str: str,
        prev_response: Optional[str] = None,
        mode: ResponseMode = ResponseMode.DEFAULT,
        **response_kwargs: Any,
    ) -> RESPONSE_TEXT_TYPE:
        """Get response."""
        # NOTE: for default and compact response modes, return synchronous version
        if mode == ResponseMode.DEFAULT:
            return self._get_response_default(query_str, prev_response)
        elif mode == ResponseMode.COMPACT:
            return self._get_response_compact(query_str, prev_response)
        elif mode == ResponseMode.TREE_SUMMARIZE:
            return await self._aget_response_tree_summarize(
                query_str, prev_response, **response_kwargs
            )
        else:
            raise ValueError(f"Invalid mode: {mode}")
