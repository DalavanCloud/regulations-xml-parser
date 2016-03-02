# -*- coding: utf-8 -*-

from __future__ import unicode_literals

from copy import deepcopy
import itertools
import logging
from lxml import etree

# Import regparser here with the eventual goal of breaking off the parts
# we're using in the RegML parser into a library both can share.
from regparser.tree.paragraph import p_levels
from regparser.tree.struct import FrozenNode
from regparser.diff.tree import changes_between

from regulation.tree import build_reg_tree


logger = logging.getLogger(__name__)


def get_parent_label(label_parts):
    """ Determine the parent label for the given label part list. """
    parent_label = None

    # It can't have a parent if it's only one part
    if len(label_parts) <= 1:
        return parent_label

    # If it's the interps for the whole part, return the part
    if len(label_parts) == 2:
        return label_parts[:1]

    # Not an interpretation label. This is easy.
    parent_label = label_parts[0:-1]

    if label_parts[-1] == 'Interp':
        # It's the whole interp for the label. Get the parent and
        # add Interp again.
        parent_label = get_parent_label(parent_label)
        parent_label.append('Interp')

    return parent_label


def get_sibling_label(label_parts):
    """ Determine the preceding sibling label for the given label part
        list, if one exists. """
    sibling_label = []

    # It can't have a sibling if it's only one part
    if len(label_parts) <= 1:
        return sibling_label

    # Start with the parent label. We don't funk interp-resolution here
    # so we won't use the get_parent_label function.
    sibling_label = label_parts[0:-1]
    last_part = label_parts[-1]
    if label_parts[-1] == 'Interp':
        # If this is an interpretation, we'll find the original marker's
        # sibling and then add 'Interp' to it at the end.
        last_part = label_parts[-2]
        sibling_label = label_parts[0:-2]

    # Now find the preceding marker for the last marker.
    for level in reversed(p_levels):
        if last_part in level:
            index = level.index(last_part)
            if index > 0:
                sibling_label.append(level[index - 1])
            else:
                # There is no preceding sibling
                return None
            break

    if label_parts[-1] == 'Interp':
        # Restore 'Interp' to the sibling label
        sibling_label.append('Interp')

    if len(sibling_label) != len(label_parts):
        # We weren't able to find the last part in the marker levels? So
        # there is no preceding sibling.
        return None

    return sibling_label


def process_changes(original_xml, notice_xml, dry=False):
    """ Process changes given in the notice_xml to modify the
        original_xml. The 'dry' param controls whether this is a
        dry run (True) or to apply the xml changes (False).
        The result is returned as a new XML tree. """

    # Copy the original XML for our new tree
    new_xml = deepcopy(original_xml)

    # Replace the fdsys and preamble with the notice preamble.
    fdsys_elm = new_xml.find('./{eregs}fdsys')
    notice_fdsys_elm = notice_xml.find('./{eregs}fdsys')
    if not dry:
        new_xml.replace(fdsys_elm, notice_fdsys_elm)

    preamble_elm = new_xml.find('./{eregs}preamble')
    notice_preamble_elm = notice_xml.find('./{eregs}preamble')
    if not dry:
        new_xml.replace(preamble_elm, notice_preamble_elm)

    # Get the changes from the notice_xml and iterate over them
    deletions = notice_xml.findall(
        './/{eregs}change[@operation="deleted"]')
    modifications = notice_xml.findall(
        './/{eregs}change[@operation="modified"]')
    additions = notice_xml.findall(
        './/{eregs}change[@operation="added"]')

    # Find the tables of contents in the original xml
    # for handy reference later when updating TOC entries
    # tocs = new_xml.findall('.//{eregs}tableOfContents')
    tocs = find_tocs(new_xml)
    logging.debug("Found {} TOCs in document".format(len(tocs)))

    # Sort them appropriately by label
    # Note: Interp labels are special. For comparison purposes, we just
    # remove the '-Interp' from the label. Otherwse we would end up with
    # something like '1234-Interp' being sorted after '1234-1-Interp'.
    get_label = lambda c: c.get('label') \
                          if 'Interp' not in c.get('label') \
                          else c.get('label').replace('-Interp', '')
    deletions = list(reversed(sorted(deletions, key=get_label)))
    modifications = list(reversed(sorted(modifications, key=get_label)))
    additions = list(sorted(additions, key=get_label))

    changes = itertools.chain(deletions, modifications, additions)
    for change in changes:
        label = change.get('label')
        op = change.get('operation')

        logging.info("Applying operation '{}' to {}".format(op, label))

        # For added labels, we need to break up the label and find its
        # parent and its preceding sibling to know where to add it.
        if op == 'added':
            label_parts = label.split('-')
            new_elm = change.getchildren()[0]
            new_index = 0

            # First make sure the label doesn't already exist
            matching_elm = new_xml.find('.//*[@label="{}"]'.format(label))
            if matching_elm is not None:
                raise KeyError("Label {} cannot be added because it "
                               "already exists. Was it added in another "
                               "change?".format(label))

            # Get the parent of the added label
            parent_label = '-'.join(get_parent_label(label_parts))
            parent_elm = new_xml.find('.//*[@label="{}"]'.format(parent_label))

            # Get the sibling of the added label
            sibling_label_parts = get_sibling_label(label_parts)
            if sibling_label_parts is not None:
                sibling_label = '-'.join(sibling_label_parts)
                sibling_elm = new_xml.find(
                    './/*[@label="{}"]'.format(sibling_label))

                # Figure out where we're inserting this element
                new_index = parent_elm.index(sibling_elm) + 1
            else:
                # If the sibling label is none, just append it to the
                # end of the parent.
                new_index = len(parent_elm.getchildren())

            # TODO: Perform TOC updates if needed
            # - Determine whether the sibling's label appears in TOC(s)
            # - If so, after the sibling's tocSecEntry, create a tocSecEntry for the new addition
            # - Insert the new tocSecEntry after the sibling's tocSecEntry

            # Insert the new xml!
            if not dry:
                parent_elm.insert(new_index, new_elm)

        if op in ('modified', 'deleted'):
            # Find a match to the given label
            matching_elm = new_xml.find('.//*[@label="{}"]'.format(label))
            if matching_elm is None:
                raise KeyError("Unable to find label {} {} in "
                               "notice.".format(label, op))

            match_parent = matching_elm.getparent()

            # For modified labels, just find the node and replace it.
            if op == 'modified':
                if len(change.getchildren()) == 0:
                    raise ValueError("Tried to modify {}, but no "
                                     "replacement given".format(label))

                # Look for whether a modified section label exists in a TOC and if so, update TOCs
                # If the label exists as a tocSecEntry target, update the sectionSubject
                # Note: This only checks for <section> tags that are in the TOC, not other children
                sections = change.findall('{eregs}section')

                logging.debug("Found {} sections in this operation".format(len(sections)))
                
                for item in sections:
                    toc_label = item.get('label')
                    toc_updates = multi_find_toc_entry(tocs, toc_label)

                    # If label doesn't appear in any TOCs, move on
                    if len(toc_updates) == 0:
                        continue

                    toc_secnum = item.get('sectionNum')
                    toc_subject = item.find('{eregs}subject').text

                    logging.debug("Found {} TOC entries for item {} ('{}'): '{}'".format(len(toc_updates),
                                                                                         toc_secnum,
                                                                                         toc_label,
                                                                                         toc_subject))

                    if not dry:
                        changed = 0
                        for toc_entry in toc_updates:
                            changed += update_toc_entry(toc_entry, toc_secnum, toc_subject)
                        logging.info("Made {} updates to TOC entries for section {} ('{}')".format(changed,
                                                                                            toc_secnum,
                                                                                            toc_label))

                if not dry:
                    new_elm = change.getchildren()[0]
                    match_parent.replace(matching_elm, new_elm)

            # For deleted labels, find the node and remove it.
            if op == 'deleted':
                
                # Look for whether a deleted label exists in TOCs and if so, delete the TOC entry
                toc_updates = multi_find_toc_entry(tocs, matching_elm)

                if not dry:
                    # Remove the TOC entries that target this label
                    changed = 0
                    for toc_entry in toc_updates:
                        delete_toc_entry(toc_entry)
                        changed += 1
                    
                    # Report how many deletions occurred
                    if changed > 0:
                        logging.info("Made {} deletions of TOC entries for item '{}'".format(changed, toc_label))

                    # Remove the element itself
                    match_parent.remove(matching_elm)

    return new_xml


def generate_diff(left_xml, right_xml):
    """ Given two full RegML trees, generate a dictionary of changes
        between the two in the style of regulations-parser.
        This wraps regulatons-parser's changes_between() function. """
    left_tree = build_reg_tree(left_xml)
    right_tree = build_reg_tree(right_xml)
    diff = dict(changes_between(FrozenNode.from_node(left_tree),
                                FrozenNode.from_node(right_tree)))
    return diff


def find_tocs(source_xml):
    """
    Finds <tableOfContents> nodes inside the source_xml and returns a list of them
    """
    return source_xml.findall('.//{eregs}tableOfContents')


def get_toc_entries(toc_root):
    """
    Retrieves a list of Table of Contents section entries (<tocSecEntry> or <tocAppEntry>)
    for the specified TOC
    """
    # Get a list of toc section entries
    sec_entries = [el for el in toc_root.iterchildren()]

    # Sort list by toc section targets - should already be sorted by target but just to be sure
    get_target = lambda c: c.get('target')
    sec_entries = list(reversed(sorted(sec_entries, key=get_target)))

    return sec_entries


def find_toc_entry(toc_root, toc_target):
    """
    Finds a Table of Contents entry by target inside the given <tableOfContents> element.

    Returns the found entry's node or None.
    """
    # Get all secEntries
    entries = get_toc_entries(toc_root)

    # Look for matching target inside
    for entry in entries:
        if entry.get('target') == toc_target:
            return entry

    # Return None if no matching target exists in the TOC
    return None


def multi_find_toc_entry(tocs, toc_target):
    """
    Finds a <tocSecEntry> by target in multiple TOCs from the list given. 
    Returns a list of all found entries or an empty list if no entries are found.
    """
    found_entries = []

    for toc in tocs:
        found = find_toc_entry(toc, toc_target)
        if found is not None:
            found_entries.append(found)

    return found_entries


def create_toc_entry(toc_parent, target_label, designator, subject, after_elm=None, is_section=True):
    """
    Inserts a new TOC entry in the toc_parent.
    
    If after_elm is specified, inserts this subelement after it; otherwise puts at the end.
    If is_section is True, inserts a section; if False inserts as an Appendix reference

    Returns the new element.
    """
    # Determine whether to insert a section or appendix reference
    if is_section:
        elm_type = "{eregs}tocSecEntry"
        num_type = "{eregs}sectionNum"
        sbj_type = "{eregs}sectionSubject"
    else: # is appendix
        elm_type = "{eregs}tocAppEntry"
        num_type = "{eregs}appendixLetter"
        sbj_type = "{eregs}appendixSubject"

    # TODO: Check to see if this target_label already exists and if so just update it?

    # Create the element and contents
    if after_elm is not None:
        new_index = toc_parent.index(after_elm) + 1
        new_elm = etree.Element(elm_type, attrib={"target":target_label})
        toc_parent.insert(new_index, new_elm)
    else:
        new_elm = etree.SubElement(toc_parent, elm_type, attrib={"target":target_label})

    # Add sub-elements
    num_elm = etree.SubElement(new_elm, num_type)
    num_elm.text = designator
    sbj_elm = etree.SubElement(new_elm, sbj_type)
    sbj_elm.text = subject

    logging.debug("Inserted new element:\n{}".format(etree.tostring(new_elm, pretty_print=True)))

    return new_elm


def update_toc_entry(toc_entry, designator, new_subject, is_section=True):
    """
    Updates the specified tocSecEntry with the given designator and subject.
    If is_section is True, inserts a section; if False inserts as an Appendix reference
    Returns whether anything changed inside the toc_entry
    """
    # Determine whether to insert a section or appendix reference
    if is_section:
        elm_type = "{eregs}tocSecEntry"
        num_type = "{eregs}sectionNum"
        sbj_type = "{eregs}sectionSubject"
    else: # is appendix
        elm_type = "{eregs}tocAppEntry"
        num_type = "{eregs}appendixLetter"
        sbj_type = "{eregs}appendixSubject"

    changed = False

    # Get references to content nodes
    num_elm = toc_entry.find(num_type)
    sbj_elm = toc_entry.find(sbj_type)

    # Check for whether the existing reference is well-formed
    if num_elm is None:
        logging.info("Found malformed {} with target '{}': Missing designator".format(elm_type, toc_entry.get('target')))
        num_elm = etree.SubElement(new_elm, num_type)
        num_elm.text = designator
        changed = True
    elif num_elm.text != designator:
        logging.debug("Updating TOC entry number: {} -> {}".format(num_elm.text, designator))
        num_elm.text = designator
        changed = True
    # else no updates required as contents already match

    if sbj_elm is None:
        logging.warning("Found malformed {} with target '{}': Missing subject".format(elm_type, toc_entry.get('target')))
        sbj_elm = etree.SubElement(new_elm, sbj_type)
        sbj_elm.text = subject
        changed = True
    elif sbj_elm.text != new_subject:
        logging.debug("Updating TOC entry contents:\nOld: '{}'\nNew: '{}'".format(sbj_elm.text, new_subject))
        sbj_elm.text = new_subject
        changed = True
    # else no updates required as contents already match

    return changed


def delete_toc_entry(toc_entry):
    """
    Deletes the specified tocSecEntry and all of its children
    """
    toc_entry.getparent().remove(toc_entry)
    
    return

