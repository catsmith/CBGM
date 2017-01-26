# encoding: utf-8

import subprocess
import sqlite3
import logging
import networkx
import string
import os
from tempfile import NamedTemporaryFile
from .shared import OL_PARENT
from .genealogical_coherence import GenealogicalCoherence
from . import mpisupport

# Colours from http://www.hitmill.com/html/pastels.html
COLOURS = ("#FF8A8A", "#FF86E3", "#FF86C2", "#FE8BF0", "#EA8DFE", "#DD88FD", "#AD8BFE",
           "#FFA4FF", "#EAA6EA", "#D698FE", "#CEA8F4", "#BCB4F3", "#A9C5EB", "#8CD1E6",
           "#8C8CFF", "#99C7FF", "#99E0FF", "#63E9FC", "#74FEF8", "#62FDCE", "#72FE95",
           "#4AE371", "#80B584", "#89FC63", "#36F200", "#66FF00", "#DFDF00", "#DFE32D")

# with open('/tmp/col.html', 'w') as f:
#     colours = ""
#     for col in COLOURS:
#         colours += '<table><tr><td bgcolor="{}">HELLO THERE {}</td></tr></table>\n'.format(col, col)
#     f.write("""<html>{}</html>""".format(colours))

COLOURMAP = {x: COLOURS[(i * 10) % len(COLOURS)]
             for (i, x) in enumerate(string.ascii_lowercase)}

logger = logging.getLogger(__name__)


def darken(col, by=75):
    """
    Darken a colour by specified amount
    """
    assert col[0] == '#'
    r, g, b = int(col[1:3], 16), int(col[3:5], 16), int(col[5:7], 16)

    def dark(x, by=by):
            new = max(x - by, 0)
            return str(hex(new))[2:]

    return '#{}{}{}'.format(dark(r), dark(g), dark(b))


class ForestError(Exception):
    pass


def get_parents(w1, w1_reading, w1_parent, variant_unit, connectivity, db_file):
    """
    Calculate the best parents for this witness at this variant unit

    Return a map of connectivity value to parent map.

    This can take a long time...
    """
    logger.info("Getting best parent(s) for {}".format(w1))

    logger.debug("Calculating genealogical coherence for {} at {}".format(w1, variant_unit))
    coh = GenealogicalCoherence(db_file, w1, variant_unit, pretty_p=False)
    coh._generate()

    logger.debug("Searching parent combinations")
    # we might need multiple parents if a reading requires it
    best_parents_by_rank = []
    best_rank = None
    best_parents_by_gen = []
    best_gen = None
    parents = []
    max_acceptable_gen = 2  # only allow my reading or my parent's
    parent_maps = {}
    for conn_value in connectivity:
        logger.info("Calculating for conn={}".format(conn_value))
        try:
            combinations = coh.parent_combinations(w1_reading, w1_parent, conn_value)
        except Exception:
            logger.exception("Couldn't get parent combinations for {}, {}, {}"
                             .format(w1_reading, w1_parent, conn_value))
            return None

        total = len(combinations)
        for i, combination in enumerate(combinations):
            count = i + 1
            if (count and not int(total / 10.0) % count) or count == total:
                # Report every 10% and at the end
                logger.debug("Done {} of {} ({:.2f}%)".format(count, total, (count / total) * 100.0))

            if not combination:
                # Couldn't find anything to explain it
                logger.info("Couldn't find any parent combination for {}".format(w1_reading))
                continue

            rank = max(x[1] for x in combination)
            gen = max(x[2] for x in combination)
            if gen > max_acceptable_gen:
                continue

            if best_gen is None or gen < best_gen:
                best_parents_by_gen = combination
                best_gen = gen
            elif gen == best_gen:
                if rank < max(x[1] for x in best_parents_by_gen):
                    # This is a better option for this generation
                    best_parents_by_gen = combination
                    best_gen = gen

            if best_rank is None or rank < best_rank:
                best_parents_by_rank = combination
                best_rank = rank

        logger.debug("Analysing results")

        if best_gen == 1:
            # We can do this with direct parents - use them
            parents = best_parents_by_gen
        else:
            # Got to use ancestors, so use the best by rank
            parents = best_parents_by_rank

        if w1_parent == OL_PARENT and not parents:
            # Top level in an overlapping unit with an omission in the initial text
            parents = [('OL_PARENT', -1, 1)]

        logger.info("Found best parents (conn={}): {}".format(conn_value, parents))
        parent_maps[conn_value] = parents

    return parent_maps


def textual_flow(db_file, variant_units, connectivity, perfect_only=False, suffix=''):
    """
    Create a textual flow diagram for the specified variant units. This will
    work out if we're using MPI and act accordingly...
    """
    if 'OMPI_COMM_WORLD_SIZE' in os.environ:
        # We have been run with mpiexec
        mpi_parent = mpisupport.is_parent()
        mpi_mode = True
    else:
        mpi_mode = False

    if mpi_mode:
        if mpi_parent:
            for i, vu in enumerate(variant_units):
                logger.debug("Running for variant unit {} ({} of {})"
                             .format(vu, i + 1, len(variant_units)))
                TextualFlow(db_file, vu, connectivity, perfect_only=False, suffix='', mpi=True)

            return TextualFlow.mpi_wait(stop=True)
        else:
            # MPI child
            mpisupport.mpi_child(get_parents)
            return "MPI child"

    else:
        for i, vu in enumerate(variant_units):
            logger.debug("Running for variant unit {} ({} of {})"
                         .format(vu, i + 1, len(variant_units)))
            TextualFlow(db_file, vu, connectivity, perfect_only=False, suffix='')


class TextualFlow(mpisupport.MpiParent):
    def __init__(self, db_file, variant_unit, connectivity, perfect_only=False, suffix='', mpi=False):
        assert type(connectivity) == list, "Connectivity must be a list"
        # Fast abort if it already exists
        self.output_files = {}
        self.connectivity = []
        for conn_value in connectivity:
            output_file = "textual_flow_{}_c{}{}.svg".format(
                variant_unit.replace('/', '_'), conn_value, suffix)

            if os.path.exists(output_file):
                logger.info("Textual flow diagram for {} already exists ({}) - skipping"
                            .format(variant_unit, output_file))
                continue

            self.output_files[conn_value] = output_file
            self.connectivity.append(conn_value)

        if not self.output_files:
            logger.info("Nothing to do - skipping variant unit {}".format(variant_unit))
            return

        # Get on and make it
        self.mpi = mpi
        if self.mpi:
            super().__init__()

        self.db_file = db_file
        self.variant_unit = variant_unit
        self.perfect_only = perfect_only
        self.suffix = suffix
        logger.debug("Initialising {}".format(self))
        self.parent_maps = {}
        self.textual_flow()

    def mpi_run(self):
        """
        Simple wrapper to handle mpi on or off.
        """
        if self.mpi:
            return super().mpi_run()
        else:
            pass

    def textual_flow(self):
        """
        Create a textual flow diagram for the specified variant unit.

        Because I put the whole textual flow in one diagram (unlike Munster who
        show a textual flow diagram for a single reading) there can be multiple
        ancestors for a witness...
        """
        logger.info("Creating textual flow diagram for {}".format(self.variant_unit))
        logger.info("Setting connectivity to {}".format(self.connectivity))
        if self.perfect_only:
            logger.info("Insisting on perfect coherence...")

        sql = """SELECT witness, label, parent
                 FROM cbgm
                 WHERE variant_unit = \"{}\"
                 """.format(self.variant_unit)
        conn = sqlite3.connect(self.db_file)
        cursor = conn.cursor()
        data = list(cursor.execute(sql))
        # get the colour for the first char of the label (e.g. for b1 just get b)
        witnesses = [(x[0], {'color': darken(COLOURMAP.get(x[1][0], '#cccccc')),
                             'fillcolor': COLOURMAP.get(x[1][0], '#cccccc'),
                             'style': 'filled'})  # See http://www.graphviz.org/
                     for x in data]

        # 1. Calculate the best parent for each witness
        for i, (w1, w1_reading, w1_parent) in enumerate(data):
            if self.mpi:
                self.mpi_queue.put((w1, w1_reading, w1_parent, self.variant_unit, self.connectivity, self.db_file))
            else:
                logger.debug("Calculating parents {}/{}".format(i, len(data)))
                parent_maps = get_parents(w1, w1_reading, w1_parent, self.variant_unit, self.connectivity, self.db_file)
                self.parent_maps[w1] = parent_maps  # a parent map per connectivity setting

        if self.mpi:
            # Wait a little for stabilisation
            logger.debug("Waiting for remote tasks")
            self.mpi_wait(stop=False)
            logger.debug("Remote tasks complete")

        # Now self.parent_maps should be complete
        logger.debug("Parent maps are: {}".format(self.parent_maps))

        # 2. Draw the diagrams
        for conn_value in self.connectivity:
            self.draw_diagram(conn_value, data, witnesses)

    def draw_diagram(self, conn_value, data, witnesses):
        """
        Draw the textual flow diagram for the specified connectivity value
        """
        G = networkx.DiGraph()
        G.add_nodes_from(witnesses)
        rank_mapping = {}
        for w1, w1_reading, w1_parent in data:
            parents = self.parent_maps[w1][conn_value]
            if parents is None:
                # Couldn't calculate them
                parents = []

            if len(parents) > 1:
                # Multiple parents - caused by a reading with multiple parents in
                # a local stemma.
                rank_mapping[w1] = "{}/[{}] ({})".format(
                    w1, ', '.join("{}.{}".format(x[0], x[1]) for x in parents), w1_reading)
            elif len(parents) == 1:
                # Just one parent
                if parents[0][1] == 1:
                    rank_mapping[w1] = "{} ({})".format(w1, w1_reading)
                else:
                    rank_mapping[w1] = "{}/{} ({})".format(w1, parents[0][1], w1_reading)
            else:
                # no parents
                rank_mapping[w1] = "{} ({})".format(w1, w1_reading)

            if all(x[0] is None for x in parents):
                if w1 == 'A':
                    # That's ok...
                    continue

                elif self.perfect_only:
                    raise ForestError("Nodes with no parents - forest detected")

                logger.warning("{} has no parents".format(w1))
                continue

            for i, p in enumerate(parents):
                G.add_edge(p[0], w1)

        # Relable nodes to include the rank
        networkx.relabel_nodes(G, rank_mapping, copy=False)

        logger.info("Creating graph with {} nodes and {} edges".format(G.number_of_nodes(),
                                                                       G.number_of_edges()))
        output_file = self.output_files[conn_value]
        with NamedTemporaryFile() as dotfile:
            networkx.write_dot(G, dotfile.name)
            subprocess.check_call(['dot', '-Tsvg', dotfile.name], stdout=open(output_file, 'w'))

        logger.info("Written to {}".format(output_file))

    def mpi_handle_result(self, args, ret):
        """
        Handle an MPI result
        @param args: original args sent to the child
        @param ret: response from the child
        """
        w1 = args[0]
        self.parent_maps[w1] = ret  # a parent map per connectivity setting
