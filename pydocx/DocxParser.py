from __future__ import (
    absolute_import,
    division,
    print_function,
    unicode_literals,
)

import logging
import posixpath

from abc import abstractmethod, ABCMeta

from pydocx import types
from pydocx.utils import (
    MulitMemoizeMixin,
    PydocxPreProcessor,
    find_all,
    find_ancestor_with_tag,
    find_first,
    get_list_style,
    has_descendant_with_tag,
)
from pydocx.exceptions import MalformedDocxException
from pydocx.wordml import WordprocessingDocument

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("NewParser")


# http://openxmldeveloper.org/discussions/formats/f/15/p/396/933.aspx
EMUS_PER_PIXEL = 9525
USE_ALIGNMENTS = True

# https://en.wikipedia.org/wiki/Twip
TWIPS_PER_POINT = 20

JUSTIFY_CENTER = 'center'
JUSTIFY_LEFT = 'left'
JUSTIFY_RIGHT = 'right'

INDENTATION_RIGHT = 'right'
INDENTATION_LEFT = 'left'
INDENTATION_FIRST_LINE = 'firstLine'


class DocxParser(MulitMemoizeMixin):
    __metaclass__ = ABCMeta
    pre_processor_class = PydocxPreProcessor

    def __init__(
        self,
        path,
        convert_root_level_upper_roman=False,
    ):
        self.path = path
        self._parsed = ''
        self.block_text = ''
        self.page_width = 0
        self.convert_root_level_upper_roman = convert_root_level_upper_roman
        self.pre_processor = None
        self.visited = set()
        self.list_depth = 0

    def _load(self):
        self.document = WordprocessingDocument(path=self.path)
        main_document_part = self.document.main_document_part
        if main_document_part is None:
            raise MalformedDocxException

        self.root_element = main_document_part.root_element
        self.numbering_root = None
        numbering_part = main_document_part.numbering_definitions_part
        if numbering_part:
            self.numbering_root = numbering_part.root_element

        pgSzEl = find_first(self.root_element, 'pgSz')
        if pgSzEl is not None:
            # pgSz is defined in twips, convert to points
            pgSz = int(pgSzEl.attrib['w'])
            self.page_width = pgSz / TWIPS_PER_POINT

        self.styles_dict = self._parse_styles()
        self.parse_begin(self.root_element)

    def _parse_run_properties(self, rPr):
        """
        Takes an `rPr` and returns a dictionary contain the tag name mapped to
        the child's value property.

        If you have an rPr that looks like this:
        <w:rPr>
            <w:b/>
            <w:u val="false"/>
            <w:sz val="16"/>
        </w:rPr>

        That will result in a dictionary that looks like this:
        {
            'b': '',
            'u': 'false',
            'sz': '16',
        }
        """
        run_properties = {}
        if rPr is None:
            return {}
        for run_property in rPr:
            val = run_property.get('val', '').lower()
            run_properties[run_property.tag] = val
        return run_properties

    def _parse_styles(self):
        styles_part = self.document.main_document_part.style_definitions_part
        if not styles_part:
            return {}
        styles_root = styles_part.root_element
        styles_dict = {}
        for style in find_all(styles_root, 'style'):
            style_val = find_first(style, 'name').attrib['val']
            run_properties = find_first(style, 'rPr')
            styles_dict[style.attrib['styleId']] = {
                'style_name': style_val,
                'default_run_properties': self._parse_run_properties(
                    run_properties,
                ),
            }
        return styles_dict

    def parse_begin(self, el):
        self.populate_memoization({
            'find_all': find_all,
            'find_first': find_first,
            'has_descendant_with_tag': has_descendant_with_tag,
            '_get_tcs_in_column': self._get_tcs_in_column,
        })

        self.pre_processor = self.pre_processor_class(
            convert_root_level_upper_roman=self.convert_root_level_upper_roman,
            styles_dict=self.styles_dict,
            numbering_root=self.numbering_root,
        )
        self.pre_processor.perform_pre_processing(el)
        self._parsed += self.parse(el)

    def parse(self, el):
        if el in self.visited:
            return ''
        self.visited.add(el)
        parsed = ''
        for child in el:
            # recursive. So you can get all the way to the bottom
            parsed += self.parse(child)
        if el.tag == 'br' and el.attrib.get('type') == 'page':
            return self.parse_page_break(el, parsed)
        elif el.tag == 'tbl':
            return self.parse_table(el, parsed)
        elif el.tag == 'tr':
            return self.parse_table_row(el, parsed)
        elif el.tag == 'tc':
            return self.parse_table_cell(el, parsed)
        elif el.tag == 'r':
            return self.parse_r(el, parsed)
        elif el.tag == 't':
            return self.parse_t(el, parsed)
        elif el.tag == 'tab':
            return self.parse_tab(el, parsed)
        elif el.tag == 'noBreakHyphen':
            return self.parse_hyphen(el, parsed)
        elif el.tag == 'br':
            return self.parse_break_tag(el, parsed)
        elif el.tag == 'delText':
            return self.parse_deletion(el, parsed)
        elif el.tag == 'p':
            return self.parse_p(el, parsed)
        elif el.tag == 'ins':
            return self.parse_insertion(el, parsed)
        elif el.tag == 'hyperlink':
            return self.parse_hyperlink(el, parsed)
        elif el.tag in ('pict', 'drawing'):
            return self.parse_image(el)
        else:
            return parsed

    def parse_page_break(self, el, text):
        # TODO figure out what parsed is getting overwritten
        return self.page_break()

    def parse_table(self, el, text):
        return self.table(text)

    def parse_table_row(self, el, text):
        return self.table_row(text)

    def parse_table_cell(self, el, text):
        v_merge = find_first(el, 'vMerge')
        if v_merge is not None and (
                'restart' != v_merge.get('val', '')):
            return ''
        colspan = self.get_colspan(el)
        rowspan = self._get_rowspan(el, v_merge)
        if rowspan > 1:
            rowspan = str(rowspan)
        else:
            rowspan = ''
        return self.table_cell(text, colspan, rowspan)

    def parse_list(self, el, text):
        """
        All the meat of building the list is done in _parse_list, however we
        call this method for two reasons: It is the naming convention we are
        following. And we need a reliable way to raise and lower the list_depth
        (which is used to determine if we are in a list). I could have done
        this in _parse_list, however it seemed cleaner to do it here.
        """
        self.list_depth += 1
        parsed = self._parse_list(el, text)
        self.list_depth -= 1
        if self.pre_processor.is_in_table(el):
            return self.parse_table_cell_contents(el, parsed)
        return parsed

    def get_list_style(self, num_id, ilvl):
        return get_list_style(self.numbering_root, num_id, ilvl)

    def _build_list(self, el, text):
        # Get the list style for the pending list.
        lst_style = self.get_list_style(
            self.pre_processor.num_id(el).num_id,
            self.pre_processor.ilvl(el),
        )

        parsed = text
        # Create the actual list and return it.
        if lst_style == 'bullet':
            return self.unordered_list(parsed)
        else:
            return self.ordered_list(
                parsed,
                lst_style,
            )

    def _parse_list(self, el, text):
        parsed = self.parse_list_item(el, text)
        num_id = self.pre_processor.num_id(el)
        ilvl = self.pre_processor.ilvl(el)
        # Everything after this point assumes the first element is not also the
        # last. If the first element is also the last then early return by
        # building and returning the completed list.
        if self.pre_processor.is_last_list_item_in_root(el):
            return self._build_list(el, parsed)
        next_el = self.pre_processor.next(el)

        def is_same_list(next_el, num_id, ilvl):
            # Bail if next_el is not an element
            if next_el is None:
                return False
            if self.pre_processor.is_last_list_item_in_root(next_el):
                return False
            # If next_el is not a list item then roll it into the list by
            # returning True.
            if not self.pre_processor.is_list_item(next_el):
                return True
            if self.pre_processor.num_id(next_el) != num_id:
                # The next element is a new list entirely
                return False
            if self.pre_processor.ilvl(next_el) < ilvl:
                # The next element is de-indented, so this is really the last
                # element in the list
                return False
            return True

        while is_same_list(next_el, num_id, ilvl):
            if next_el in self.visited:
                # Early continue for elements we have already visited.
                next_el = self.pre_processor.next(next_el)
                continue

            if self.pre_processor.is_list_item(next_el):
                # Reset the ilvl
                ilvl = self.pre_processor.ilvl(next_el)

            parsed += self.parse(next_el)
            next_el = self.pre_processor.next(next_el)

        def should_parse_last_el(last_el, first_el):
            if last_el is None:
                return False
            # Different list
            if (
                    self.pre_processor.num_id(last_el) !=
                    self.pre_processor.num_id(first_el)):
                return False
            # Will be handled when the ilvls do match (nesting issue)
            if (
                    self.pre_processor.ilvl(last_el) !=
                    self.pre_processor.ilvl(first_el)):
                return False
            # We only care about last items that have not been parsed before
            # (first list items are always parsed at the beginning of this
            # method.)
            return (
                not self.pre_processor.is_first_list_item(last_el) and
                self.pre_processor.is_last_list_item_in_root(last_el)
            )
        if should_parse_last_el(next_el, el):
            parsed += self.parse(next_el)

        # If the list has no content, then we don't need to worry about the
        # list styling, because it will be stripped out.
        if parsed == '':
            return parsed

        return self._build_list(el, parsed)

    def justification(self, el, text):
        paragraph_tag_property = el.find('pPr')
        if paragraph_tag_property is None:
            return text

        jc = paragraph_tag_property.find('jc')
        indentation = paragraph_tag_property.find('ind')
        if jc is None and indentation is None:
            return text
        alignment = None
        right = None
        left = None
        firstLine = None
        if jc is not None:  # text alignments
            value = jc.attrib['val']
            if value in [JUSTIFY_LEFT, JUSTIFY_CENTER, JUSTIFY_RIGHT]:
                alignment = value

        if indentation is not None:
            if INDENTATION_RIGHT in indentation.attrib:
                right = int(indentation.attrib[INDENTATION_RIGHT])
            if INDENTATION_LEFT in indentation.attrib:
                left = int(indentation.attrib[INDENTATION_LEFT])
            if INDENTATION_FIRST_LINE in indentation.attrib:
                firstLine = int(indentation.attrib[INDENTATION_FIRST_LINE])
        if any([alignment, firstLine, left, right]):
            return self.indent(text, alignment, firstLine, left, right)
        return text

    def parse_p(self, el, text):
        if text == '':
            return ''
        # TODO This is still not correct, however it fixes the bug. We need to
        # apply the classes/styles on p, td, li and h tags instead of inline,
        # but that is for another ticket.
        text = self.justification(el, text)
        if self.pre_processor.is_first_list_item(el):
            return self.parse_list(el, text)
        if self.pre_processor.heading_level(el):
            return self.parse_heading(el, text)
        if self.pre_processor.is_list_item(el):
            return self.parse_list_item(el, text)
        if self.pre_processor.is_in_table(el):
            return self.parse_table_cell_contents(el, text)
        parsed = text
        # No p tags in li tags
        if self.list_depth == 0:
            parsed = self.paragraph(parsed)
        return parsed

    def _should_append_break_tag(self, next_el):
        paragraph_like_tags = [
            'p',
        ]
        inline_like_tags = [
            'smartTag',
            'ins',
            'delText',
        ]
        if self.pre_processor.is_list_item(next_el):
            return False
        if self.pre_processor.previous(next_el) is None:
            return False
        tag_is_inline_like = any(
            self.memod_tree_op('has_descendant_with_tag', next_el, tag) for
            tag in inline_like_tags
        )
        if tag_is_inline_like:
            return False
        if (
                self.pre_processor.is_last_list_item_in_root(
                    self.pre_processor.previous(next_el))):
            return False
        if self.pre_processor.previous(next_el).tag not in paragraph_like_tags:
            return False
        if next_el.tag not in paragraph_like_tags:
            return False
        return True

    def parse_heading(self, el, parsed):
        return self.heading(parsed, self.pre_processor.heading_level(el))

    def parse_list_item(self, el, text):
        # If for whatever reason we are not currently in a list, then start
        # a list here. This will only happen if the num_id/ilvl combinations
        # between lists is not well formed.
        parsed = text
        if self.list_depth == 0:
            return self.parse_list(el, parsed)

        def _should_parse_next_as_content(el):
            """
            Get the contents of the next el and append it to the
            contents of the current el (that way things like tables
            are actually in the li tag instead of in the ol/ul tag).
            """
            next_el = self.pre_processor.next(el)
            if next_el is None:
                return False
            if (
                    not self.pre_processor.is_list_item(next_el) and
                    not self.pre_processor.is_last_list_item_in_root(el)
            ):
                return True
            if self.pre_processor.is_first_list_item(next_el):
                if (
                        self.pre_processor.num_id(next_el) ==
                        self.pre_processor.num_id(el)):
                    return True
            return False

        while el is not None:
            if _should_parse_next_as_content(el):
                el = self.pre_processor.next(el)
                next_elements_content = self.parse(el)
                if not next_elements_content:
                    continue
                if self._should_append_break_tag(el):
                    parsed += self.break_tag()
                parsed += next_elements_content
            else:
                break
        # Create the actual li element
        return self.list_element(parsed)

    def _get_tcs_in_column(self, tbl, column_index):
        return [
            tc for tc in self.memod_tree_op('find_all', tbl, 'tc')
            if self.pre_processor.column_index(tc) == column_index
        ]

    def _get_rowspan(self, el, v_merge):
        restart_in_v_merge = False
        if v_merge is not None and 'val' in v_merge.attrib:
            restart_in_v_merge = 'restart' in v_merge.attrib['val']

        if not restart_in_v_merge:
            return -1

        current_row = self.pre_processor.row_index(el)
        current_col = self.pre_processor.column_index(el)
        rowspan = 1
        result = -1
        tbl = find_ancestor_with_tag(self.pre_processor, el, 'tbl')
        # We only want table cells that have a higher row_index that is greater
        # than the current_row and that are on the current_col
        if tbl is None:
            return -1

        tcs = [
            tc for tc in self.memod_tree_op(
                '_get_tcs_in_column', tbl, current_col,
            ) if self.pre_processor.row_index(tc) >= current_row
        ]

        def should_increment_rowspan(tc):
            if not self.pre_processor.vmerge_continue(tc):
                return False
            return True

        for tc in tcs:
            if should_increment_rowspan(tc):
                rowspan += 1
            else:
                rowspan = 1
            if rowspan > 1:
                result = rowspan
        return result

    def get_colspan(self, el):
        grid_span = find_first(el, 'gridSpan')
        if grid_span is None:
            return ''
        return grid_span.attrib['val']

    def parse_table_cell_contents(self, el, text):
        parsed = text

        next_el = self.pre_processor.next(el)
        if next_el is not None:
            if self._should_append_break_tag(next_el):
                parsed += self.break_tag()
        return parsed

    def parse_hyperlink(self, el, text):
        relationship_id = el.get('id')
        package_part = self.document.main_document_part.package_part
        try:
            relationship = package_part.get_relationship(
                relationship_id=relationship_id,
            )
        except KeyError:
            # Preserve the text even if we are unable to resolve the hyperlink
            return text
        href = self.escape(relationship.target_uri)
        return self.hyperlink(text, href)

    def _get_image_id(self, el):
        # Drawings
        blip = find_first(el, 'blip')
        if blip is not None:
            # On drawing tags the id is actually whatever is returned from the
            # embed attribute on the blip tag. Thanks a lot Microsoft.
            return blip.get('embed')
        # Picts
        imagedata = find_first(el, 'imagedata')
        if imagedata is not None:
            return imagedata.get('id')

    def _convert_image_size(self, size):
        return size / EMUS_PER_PIXEL

    def _get_image_size(self, el):
        """
        If we can't find a height or width, return 0 for whichever is not
        found, then rely on the `image` handler to strip those attributes. This
        functionality can change once we integrate PIL.
        """
        sizes = find_first(el, 'ext')
        if sizes is not None and sizes.get('cx'):
            if sizes.get('cx'):
                x = self._convert_image_size(int(sizes.get('cx')))
            if sizes.get('cy'):
                y = self._convert_image_size(int(sizes.get('cy')))
            return (
                '%dpx' % x,
                '%dpx' % y,
            )
        shape = find_first(el, 'shape')
        if shape is not None and shape.get('style') is not None:
            # If either of these are not set, rely on the method `image` to not
            # use either of them.
            x = 0
            y = 0
            styles = shape.get('style').split(';')

            for s in styles:
                if s.startswith('height:'):
                    y = s.split(':')[1]
                if s.startswith('width:'):
                    x = s.split(':')[1]
            return x, y
        return 0, 0

    def parse_image(self, el):
        x, y = self._get_image_size(el)
        relationship_id = self._get_image_id(el)
        try:
            image_part = self.document.main_document_part.get_part_by_id(
                relationship_id=relationship_id,
            )
        except KeyError:
            return ''
        data = image_part.stream.read()
        _, filename = posixpath.split(image_part.uri)
        return self.image(
            data,
            filename,
            x,
            y,
        )

    def parse_t(self, el, parsed):
        if el.text is None:
            return ''
        return self.escape(el.text)

    def parse_tab(self, el, parsed):
        return self.tab()

    def parse_hyphen(self, el, parsed):
        return '-'

    def parse_break_tag(self, el, parsed):
        return self.break_tag()

    def parse_deletion(self, el, parsed):
        if el.text is None:
            return ''
        return self.deletion(el.text, '', '')

    def parse_insertion(self, el, parsed):
        return self.insertion(parsed, '', '')

    def parse_r(self, el, parsed):
        """
        Parse the running text.
        """
        text = parsed
        if not text:
            return ''

        run_properties = {}

        # Get the rPr for the current style, they are the defaults.
        p = find_ancestor_with_tag(self.pre_processor, el, 'p')
        paragraph_style = self.memod_tree_op('find_first', p, 'pStyle')
        if paragraph_style is not None:
            style = paragraph_style.get('val')
            style_defaults = self.styles_dict.get(style, {})
            run_properties.update(
                style_defaults.get('default_run_properties', {}),
            )

        # Get the rPr for the current r tag, they are overrides.
        run_properties_element = el.find('rPr')
        if run_properties_element:
            local_run_properties = self._parse_run_properties(
                run_properties_element,
            )
            run_properties.update(local_run_properties)

        inline_tag_types = {
            'b': types.OnOff,
            'i': types.OnOff,
            'u': types.Underline,
            'caps': types.OnOff,
            'smallCaps': types.OnOff,
            'strike': types.OnOff,
            'dstrike': types.OnOff,
            'vanish': types.OnOff,
            'webHidden': types.OnOff,
        }

        inline_tag_handlers = {
            'b': self.bold,
            'i': self.italics,
            'u': self.underline,
            'caps': self.caps,
            'smallCaps': self.small_caps,
            'strike': self.strike,
            'dstrike': self.strike,
            'vanish': self.hide,
            'webHidden': self.hide,
        }
        styles_needing_application = []
        for property_name, property_value in run_properties.items():
            # These tags are a little different, handle them separately
            # from the rest.
            # This could be a superscript or a subscript
            if property_name == 'vertAlign':
                if property_value == 'superscript':
                    styles_needing_application.append(self.superscript)
                elif property_value == 'subscript':
                    styles_needing_application.append(self.subscript)
            else:
                if (
                    property_name in inline_tag_handlers and
                    inline_tag_types.get(property_name)(property_value)
                ):
                    styles_needing_application.append(
                        inline_tag_handlers[property_name],
                    )

        # Apply all the handlers.
        for func in styles_needing_application:
            text = func(text)

        return text

    @property
    def parsed(self):
        if not self._parsed:
            self._load()
        return self._parsed

    @property
    def escape(self, text):
        return text

    @abstractmethod
    def linebreak(self):
        return ''

    @abstractmethod
    def paragraph(self, text):
        return text

    @abstractmethod
    def heading(self, text, heading_level):
        return text

    @abstractmethod
    def insertion(self, text, author, date):
        return text

    @abstractmethod
    def hyperlink(self, text, href):
        return text

    @abstractmethod
    def image_handler(self, image_data, path):
        return path

    @abstractmethod
    def image(self, data, filename, x, y):
        return self.image_handler(data, filename)

    @abstractmethod
    def deletion(self, text, author, date):
        return text

    @abstractmethod
    def bold(self, text):
        return text

    @abstractmethod
    def italics(self, text):
        return text

    @abstractmethod
    def underline(self, text):
        return text

    @abstractmethod
    def caps(self, text):
        return text

    @abstractmethod
    def small_caps(self, text):
        return text

    @abstractmethod
    def strike(self, text):
        return text

    @abstractmethod
    def hide(self, text):
        return text

    @abstractmethod
    def superscript(self, text):
        return text

    @abstractmethod
    def subscript(self, text):
        return text

    @abstractmethod
    def tab(self):
        return True

    @abstractmethod
    def ordered_list(self, text):
        return text

    @abstractmethod
    def unordered_list(self, text):
        return text

    @abstractmethod
    def list_element(self, text):
        return text

    @abstractmethod
    def table(self, text):
        return text

    @abstractmethod
    def table_row(self, text):
        return text

    @abstractmethod
    def table_cell(self, text):
        return text

    @abstractmethod
    def page_break(self):
        return True

    @abstractmethod
    def indent(
        self,
        text,
        alignment=None,
        left=None,
        right=None,
        firstLine=None,
    ):
        return text
