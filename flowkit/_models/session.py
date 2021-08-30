"""
Session class
"""
import io
import sys
import copy
import numpy as np
import pandas as pd
from bokeh.models import Title
from .._models.gating_strategy import GatingStrategy
# noinspection PyProtectedMember
from .._models.transforms._matrix import Matrix
from .._models import gates
from .._models.sample import Sample
from .._utils.gate_utils import multi_proc, mp
from .._utils import plot_utils, xml_utils, wsp_utils, sample_utils
import warnings


# Used to detect PyCharm's debugging mode to turn off multi-processing for debugging with tests
get_trace = getattr(sys, 'gettrace', lambda: None)

if get_trace() is None:
    use_mp = True
else:
    use_mp = False


# _gate_sample & _gate_samples are multi-proc wrappers for GatingStrategy _gate_sample method
# These are functions external to GatingStrategy as mp doesn't work well for class methods
def _gate_sample(data):
    gating_strategy = data[0]
    sample = data[1]
    verbose = data[2]
    return gating_strategy.gate_sample(sample, verbose=verbose)


def _gate_samples(gating_strategies, samples, verbose, use_mp=False):
    # TODO: Multiprocessing can fail for very large workloads (lots of gates), maybe due
    #       to running out of memory. Will investigate further, but for now setting an
    #       option to control whether mp is used (default is True)
    #       Needs further investigation, as this is still causing problems.
    sample_count = len(samples)
    if multi_proc and sample_count > 1 and use_mp:
        if sample_count < mp.cpu_count():
            proc_count = sample_count
        else:
            proc_count = mp.cpu_count() - 1  # leave a CPU free just to be nice

        with mp.get_context("spawn").Pool(processes=proc_count) as pool:
            data = [(gating_strategies[i], sample, verbose) for i, sample in enumerate(samples)]
            all_results = pool.map(_gate_sample, data)

            pool.close()
            pool.join()
    else:
        all_results = []
        for i, sample in enumerate(samples):
            results = gating_strategies[i].gate_sample(sample, verbose=verbose)
            all_results.append(results)

    return all_results


class Session(object):
    """
    The Session class is intended as the main interface in FlowKit for complex flow cytometry analysis.
    A Session represents a collection of gating strategies and FCS samples. FCS samples are added and assigned to sample
    groups, and each sample group has a single gating strategy template. The gates in a template can be customized
    per sample.

    :param fcs_samples: a list of either file paths or Sample instances
    :param subsample_count: Number of events to use as a sub-sample. If the number of
        events in the Sample is less than the requested sub-sample count, then the
        maximum number of available events is used for the sub-sample.
    """
    def __init__(self, fcs_samples=None, subsample_count=10000):
        self.subsample_count = subsample_count
        self.sample_lut = {}
        self._results_lut = {}
        self._sample_group_lut = {}

        self.add_sample_group('default')

        self.add_samples(fcs_samples)

    def add_sample_group(self, group_name, gating_strategy=None):
        """
        Create a new sample group to the session. The group name must be unique to the session.

        :param group_name: a text string representing the sample group
        :param gating_strategy: an optional gating strategy to use for the group template. Can be
            a path or file to a GatingML 2.0 file or a GatingStrategy instance. If None, then a new,
            blank gating strategy will be created.
        :return: None
        """
        if group_name in self._sample_group_lut:
            warnings.warn("A sample group with this name already exists...skipping")
            return

        if isinstance(gating_strategy, GatingStrategy):
            gating_strategy = gating_strategy
        elif isinstance(gating_strategy, str) or isinstance(gating_strategy, io.IOBase):
            # assume a path to an XML file representing either a GatingML document or FlowJo workspace
            gating_strategy = xml_utils.parse_gating_xml(gating_strategy)
        elif gating_strategy is None:
            gating_strategy = GatingStrategy()
        else:
            raise ValueError(
                "'gating_strategy' must be either a GatingStrategy instance or a path to a GatingML document"
            )

        self._sample_group_lut[group_name] = {
            'template': gating_strategy,
            'samples': {}
        }

    def import_flowjo_workspace(
            self,
            workspace_file_or_path,
            ignore_missing_files=False,
            ignore_transforms=False
    ):
        """
        Imports a FlowJo workspace (version 10+) into the Session. Each sample group in the workspace will
        be a sample group in the FlowKit session. Referenced samples in the workspace will be imported as
        references in the session. Ideally, these samples should have already been loaded into the session,
        and a warning will be issued for each sample reference that has not yet been loaded.
        Support for FlowJo workspaces is limited to the following
        features:

          - Transformations:

            - linear
            - log
            - logicle
          - Gates:

            - rectangle
            - polygon
            - ellipse
            - quadrant
            - range

        :param workspace_file_or_path: WSP workspace file as a file name/path, file object, or file-like object
        :param ignore_missing_files: Controls whether UserWarning messages are issued for FCS files found in the
            workspace that have not yet been loaded in the Session. Default is False, displaying warnings.
        :param ignore_transforms: Controls whether transformations are applied to the gate definitions within the
            FlowJo workspace. Useful for extracting gate vertices in the un-transformed space. Default is False.
        :return: None
        """
        wsp_sample_groups = wsp_utils.parse_wsp(workspace_file_or_path, ignore_transforms=ignore_transforms)
        for group_name, sample_data in wsp_sample_groups.items():
            for sample, data_dict in sample_data.items():
                if sample not in self.sample_lut:
                    self.sample_lut[sample] = None
                    if not ignore_missing_files:
                        msg = "Sample %s has not been added to the session. \n" % sample
                        msg += "A GatingStrategy was loaded for this sample ID, but the file needs to be added " \
                               "to the Session prior to running the analyze_samples method."
                        warnings.warn(msg)

                gs = GatingStrategy()

                for gate_dict in data_dict['gates']:
                    gs.add_gate(gate_dict['gate'], gate_path=gate_dict['gate_path'])

                matrix = data_dict['compensation']
                if isinstance(matrix, Matrix):
                    gs.comp_matrices[matrix.id] = matrix
                gs.transformations = {xform.id: xform for xform in data_dict['transforms']}

                if group_name not in self._sample_group_lut:
                    self.add_sample_group(group_name, gs)

                self._sample_group_lut[group_name]['samples'][sample] = gs

    def add_samples(self, samples):
        """
        Adds FCS samples to the session. All added samples will be added to the 'default' sample group.

        :param samples: a list of Sample instances
        :return: None
        """
        # TODO: Using mp here causes problems when running tests in debug mode. Needs further investigation.
        new_samples = sample_utils.load_samples(samples, use_mp=use_mp)
        for s in new_samples:
            s.subsample_events(self.subsample_count)
            if s.original_filename in self.sample_lut:
                # sample ID may have been added via a FlowJo workspace,
                # check if Sample value is None
                if self.sample_lut[s.original_filename] is not None:
                    warnings.warn("A sample with ID %s already exists...skipping" % s.original_filename)
                    continue
            self.sample_lut[s.original_filename] = s

            # all samples get added to the 'default' group
            self.assign_sample(s.original_filename, 'default')

    def assign_sample(self, sample_id, group_name):
        """
        Assigns a sample ID to a sample group. Samples can belong to more than one sample group.

        :param sample_id: Sample ID to assign to the specified sample group
        :param group_name: name of sample group to which the sample will be assigned
        :return: None
        """
        group = self._sample_group_lut[group_name]
        if sample_id in group['samples']:
            warnings.warn("Sample %s is already assigned to the group %s...nothing changed" % (sample_id, group_name))
            return
        template = group['template']
        group['samples'][sample_id] = copy.deepcopy(template)

    def get_sample_ids(self, loaded_only=True):
        """
        Retrieve the list of Sample IDs that have been loaded or referenced in the Session.

        :param loaded_only: only return IDs for samples loaded in the Session (relevant
            when a FlowJo workspace was imported without samples)

        :return: list of Sample ID strings
        """
        if loaded_only:
            sample_ids = []

            for k, v in self.sample_lut.items():
                if isinstance(v, Sample):
                    sample_ids.append(k)
        else:
            sample_ids = list(self.sample_lut.keys())

        return sample_ids

    def get_sample_groups(self):
        """
        Retrieve the list of sample group labels defined in the Session.

        :return: list of sample group ID strings
        """
        return list(self._sample_group_lut.keys())

    def get_group_sample_ids(self, group_name, loaded_only=True):
        """
        Retrieve the list of Sample IDs belonging to the specified sample group.
        
        :param group_name: a text string representing the sample group
        :param loaded_only: only return IDs for samples loaded in the Session (relevant
            when a FlowJo workspace was imported without samples)
        :return: list of Sample IDs
        """
        # convert to list instead of dict_keys
        sample_ids = list(self._sample_group_lut[group_name]['samples'].keys())
        if loaded_only:
            loaded_sample_ids = self.get_sample_ids()
            sample_ids = list(set(sample_ids).union(set(loaded_sample_ids)))
        else:
            sample_ids = list(self._sample_group_lut[group_name]['samples'].keys())

        return sample_ids

    def get_group_samples(self, group_name):
        """
        Retrieve the list of Sample instances belonging to the specified sample group.
        Only samples that have been loaded into the Session are returned.

        :param group_name: a text string representing the sample group
        :return: list of Sample instances
        """
        # don't return samples that haven't been loaded
        sample_ids = self.get_group_sample_ids(group_name, loaded_only=True)

        samples = []
        for s_id in sample_ids:
            sample = self.sample_lut[s_id]
            samples.append(sample)

        return samples

    def get_gate_ids(self, group_name):
        """
        Retrieve the list of gate IDs defined in the specified sample group
        :param group_name: a text string representing the sample group
        :return: list of gate ID strings
        """
        group = self._sample_group_lut[group_name]
        template = group['template']
        return template.get_gate_ids()

    # start pass through methods for GatingStrategy class
    def add_gate(self, gate, gate_path=None, group_name='default'):
        """
        Add a Gate instance to a sample group in the session. Gates will be added to
        the 'default' sample group by default.

        :param gate: an instance of a Gate sub-class
        :param gate_path: complete list of gate IDs for unique set of gate ancestors.
            Required if gate.id and gate.parent combination is ambiguous
        :param group_name: a text string representing the sample group
        :return: None
        """
        group = self._sample_group_lut[group_name]
        template = group['template']
        s_members = group['samples']

        # first, add gate to template, then add a copy to each group sample gating strategy
        template.add_gate(copy.deepcopy(gate), gate_path=gate_path)
        for s_id, s_strategy in s_members.items():
            s_strategy.add_gate(copy.deepcopy(gate), gate_path=gate_path)

    def add_transform(self, transform, group_name='default'):
        """
        Add a Transform instance to a sample group in the session. Transforms will be added to
        the 'default' sample group by default.

        :param transform: an instance of a Transform sub-class
        :param group_name: a text string representing the sample group
        :return: None
        """
        group = self._sample_group_lut[group_name]
        template = group['template']
        s_members = group['samples']

        # first, add gate to template, then add a copy to each group sample gating strategy
        template.add_transform(copy.deepcopy(transform))
        for s_id, s_strategy in s_members.items():
            s_strategy.add_transform(copy.deepcopy(transform))

    def get_group_transforms(self, group_name):
        """
        Retrieve the list of Transform instances stored within the specified
        sample group.

        :param group_name: a text string representing the sample group
        :return: list of Transform instances
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['template']

        return list(gating_strategy.transformations.values())

    def get_transform(self, group_name, transform_id):
        """
        Retrieve a Transform instance stored within the specified
        sample group associated with the given sample ID & having the given transform ID.

        :param group_name: a text string representing the sample group
        :param transform_id: a text string representing a Transform ID
        :return: an instance of a Transform sub-class
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['template']
        xform = gating_strategy.get_transform(transform_id)

        return xform

    def add_comp_matrix(self, matrix, group_name='default'):
        """
        Add a Matrix instance to a sample group in the session. Matrices will be added to
        the 'default' sample group by default.

        :param matrix: an instance of the Matrix class
        :param group_name: a text string representing the sample group
        :return: None
        """
        group = self._sample_group_lut[group_name]
        template = group['template']
        s_members = group['samples']

        # first, add gate to template, then add a copy to each group sample gating strategy
        template.add_comp_matrix(copy.deepcopy(matrix))
        for s_id, s_strategy in s_members.items():
            s_strategy.add_comp_matrix(copy.deepcopy(matrix))

    def get_group_comp_matrices(self, group_name):
        """
        Retrieve the list of compensation Matrix instances stored within the specified
        sample group.

        :param group_name: a text string representing the sample group
        :return: list of Matrix instances
        """
        group = self._sample_group_lut[group_name]
        comp_matrices = []

        for sample_id in group['samples']:
            gating_strategy = group['samples'][sample_id]
            comp_matrices.extend(list(gating_strategy.comp_matrices.values()))

        return comp_matrices

    def get_comp_matrix(self, group_name, sample_id, matrix_id):
        """
        Retrieve a compensation Matrix instance stored within the specified
        sample group associated with the given sample ID & having the given matrix ID.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :param matrix_id: a text string representing a Matrix ID
        :return: a Matrix instance
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['samples'][sample_id]
        comp_mat = gating_strategy.get_comp_matrix(matrix_id)
        return comp_mat

    def get_parent_gate_id(self, group_name, gate_id):
        """
        Retrieve a parent gate instance by the child gate ID, sample group, and sample ID.

        :param group_name: a text string representing the sample group
        :param gate_id: text string of a gate ID
        :return: Subclass of a Gate object
        """
        # TODO: this needs to handle getting default template gate or sample specific gate
        group = self._sample_group_lut[group_name]
        template = group['template']
        gate = template.get_gate(gate_id)
        return gate.parent

    def get_gate(self, group_name, gate_id, gate_path=None, sample_id=None):
        """
        Retrieve a gate instance by its group, sample, and gate ID.

        :param group_name: a text string representing the sample group
        :param gate_id: text string of a gate ID
        :param gate_path: complete list of gate IDs for unique set of gate ancestors. Required if gate_id is ambiguous
        :param sample_id: a text string representing a Sample instance. If None, the template gate is returned.
        :return: Subclass of a Gate object
        """
        # TODO: this needs to handle getting default template gate or sample specific gate
        group = self._sample_group_lut[group_name]
        if sample_id is None:
            gating_strategy = group['template']
        else:
            gating_strategy = group['samples'][sample_id]
        gate = gating_strategy.get_gate(gate_id, gate_path=gate_path)
        return gate

    def get_sample_gates(self, group_name, sample_id):
        """
        Retrieve all gates for a sample in a sample group.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :return: list of Gate sub-class instances
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['samples'][sample_id]
        gate_tuples = gating_strategy.get_gate_ids()

        sample_gates = []

        for gate_id, ancestors in gate_tuples:
            gate = gating_strategy.get_gate(gate_id, gate_path=ancestors)
            sample_gates.append(gate)

        return sample_gates

    def get_sample_comp_matrices(self, group_name, sample_id):
        """
        Retrieve all compensation matrices for a sample in a sample group.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :return: list of Matrix instances
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['samples'][sample_id]

        return list(gating_strategy.comp_matrices.values())

    def get_sample_transforms(self, group_name, sample_id):
        """
        Retrieve all Transform instances for a sample in a sample group.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :return: list of Transform sub-class instances
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['samples'][sample_id]

        return list(gating_strategy.transformations.values())

    def get_gate_hierarchy(self, group_name, output='ascii', **kwargs):
        """
        Retrieve the hierarchy of gates in the sample group's gating strategy. Output is available
        in several formats, including text, dictionary, or JSON. If output == 'json', extra
        keyword arguments are passed to json.dumps

        :param group_name: a text string representing the sample group
        :param output: Determines format of hierarchy returned, either 'ascii',
            'dict', or 'JSON' (default is 'ascii')
        :return: gate hierarchy as a text string or a dictionary
        """
        return self._sample_group_lut[group_name]['template'].get_gate_hierarchy(output, **kwargs)
    # end pass through methods for GatingStrategy

    def export_gml(self, file_handle, group_name, sample_id=None):
        """
        Export a GatingML 2.0 file for the specified sample group and sample ID

        :param file_handle: file handle for exporting data
        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :return: None
        """
        group = self._sample_group_lut[group_name]
        if sample_id is not None:
            gating_strategy = group['samples'][sample_id]
        else:
            gating_strategy = group['template']
        xml_utils.export_gatingml(gating_strategy, file_handle)

    def export_wsp(self, file_handle, group_name):
        """
        Export a FlowJo 10 workspace file (.wsp) for the specified sample group

        :param file_handle: file handle for exporting data
        :param group_name: a text string representing the sample group
        :return: None
        """
        group_gating_strategies = self._sample_group_lut[group_name]
        samples = self.get_group_samples(group_name)

        wsp_utils.export_flowjo_wsp(group_gating_strategies, group_name, samples, file_handle)

    def get_sample(self, sample_id):
        """
        Retrieve a Sample instance from the Session
        :param sample_id: a text string representing the sample
        :return: Sample instance
        """
        return self.sample_lut[sample_id]

    def analyze_samples(self, group_name='default', sample_id=None, verbose=False):
        """
        Process gates for samples in a sample group. After running, results can be
        retrieved using the `get_gating_results`, `get_group_report`, and  `get_gate_indices`,
        methods.

        :param group_name: a text string representing the sample group
        :param sample_id: optional sample ID, if specified only this sample will be processed
        :param verbose: if True, print a line for every gate processed (default is False)
        :return: None
        """
        # Don't save just the DataFrame report, save the entire
        # GatingResults objects for each sample, since we'll need the gate
        # indices for each sample.
        samples = self.get_group_samples(group_name)
        if len(samples) == 0:
            warnings.warn("No samples have been assigned to sample group %s" % group_name)
            return

        if sample_id is not None:
            sample_ids = self.get_group_sample_ids(group_name)
            if sample_id not in sample_ids:
                warnings.warn("%s is not assigned to sample group %s" % (sample_id, group_name))
                return

            samples = [self.get_sample(sample_id)]

        gating_strategies = []
        samples_to_run = []
        for s in samples:
            if s is None:
                # sample hasn't been added to Session
                continue
            gating_strategies.append(self._sample_group_lut[group_name]['samples'][s.original_filename])
            samples_to_run.append(s)

        results = _gate_samples(gating_strategies, samples_to_run, verbose, use_mp=use_mp)

        all_reports = [res.report for res in results]

        self._results_lut[group_name] = {
            'report': pd.concat(all_reports),
            'samples': {}  # dict will have sample ID keys and results values
        }
        for r in results:
            self._results_lut[group_name]['samples'][r.sample_id] = r

    def get_gating_results(self, group_name, sample_id):
        """
        Retrieve analyzed gating results gates for a sample in a sample group.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a loaded Sample instance that is
            assigned to the specified group
        :return: GatingResults instance
        """

        gating_result = self._results_lut[group_name]['samples'][sample_id]
        return copy.deepcopy(gating_result)

    def get_group_report(self, group_name):
        """
        Retrieve the report of an analyzed sample group as a Pandas DataFrame.

        :param group_name: a text string representing the sample group
        :return: Pandas DataFrame
        """
        return self._results_lut[group_name]['report']

    def get_gate_indices(self, group_name, sample_id, gate_id, gate_path=None):
        """
        Retrieve a boolean array indicating gate membership for the events in the
        specified sample. Note, the same gate ID may be found in multiple gate paths,
        i.e. the gate ID can be ambiguous. In this case, specify the full gate path
        to retrieve gate indices.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :param gate_id: text string of a gate ID
        :param gate_path: complete list of gate IDs for unique set of gate ancestors.
            Required if gate_id is ambiguous
        :return: NumPy boolean array (length of sample event count)
        """
        gating_result = self._results_lut[group_name]['samples'][sample_id]
        return gating_result.get_gate_indices(gate_id, gate_path=gate_path)

    def get_gate_events(self, group_name, sample_id, gate_id, gate_path=None, matrix=None, transform=None):
        """
        Retrieve a Pandas DataFrame containing only the events within the specified gate.
        If an optional compensation matrix and/or a transform is provided, the returned
        event data will be compensated or transformed. If both a compensation matrix and
        a transform is provided the event data will be both compensated and transformed.

        :param group_name: a text string representing the sample group
        :param sample_id: a text string representing a Sample instance
        :param gate_id: text string of a gate ID
        :param gate_path: complete list of gate IDs for unique set of gate ancestors.
            Required if gate_id is ambiguous
        :param matrix: an instance of the Matrix class
        :param transform: an instance of a Transform sub-class
        :return: Pandas DataFrame containing only the events within the specified gate
        """
        gate_idx = self.get_gate_indices(group_name, sample_id, gate_id, gate_path)
        sample = self.get_sample(sample_id)
        sample = copy.deepcopy(sample)

        # default is 'raw' events
        event_source = 'raw'

        if matrix is not None:
            sample.apply_compensation(matrix)
            event_source = 'comp'
        if transform is not None:
            sample.apply_transform(transform)
            event_source = 'xform'

        events_df = sample.as_dataframe(source=event_source)
        gated_event_data = events_df[gate_idx]

        return gated_event_data

    def plot_gate(
            self,
            group_name,
            sample_id,
            gate_id,
            gate_path=None,
            x_min=None,
            x_max=None,
            y_min=None,
            y_max=None,
            color_density=True
    ):
        """
        Returns an interactive plot for the specified gate. The type of plot is determined by the number of
         dimensions used to define the gate: single dimension gates will be histograms, 2-D gates will be returned
         as a scatter plot.

        :param group_name: The sample group containing the sample ID (and, optionally the gate ID)
        :param sample_id: The sample ID for the FCS sample to plot
        :param gate_id: Gate ID to filter events (only events within the given gate will be plotted)
        :param gate_path: list of gate IDs for full set of gate ancestors.
            Required if gate_id is ambiguous
        :param x_min: Lower bound of x-axis. If None, channel's min value will
            be used with some padding to keep events off the edge of the plot.
        :param x_max: Upper bound of x-axis. If None, channel's max value will
            be used with some padding to keep events off the edge of the plot.
        :param y_min: Lower bound of y-axis. If None, channel's min value will
            be used with some padding to keep events off the edge of the plot.
        :param y_max: Upper bound of y-axis. If None, channel's max value will
            be used with some padding to keep events off the edge of the plot.
        :param color_density: Whether to color the events by density, similar
            to a heat map. Default is True.
        :return: A Bokeh Figure object containing the interactive scatter plot.
        """
        group = self._sample_group_lut[group_name]
        gating_strategy = group['samples'][sample_id]
        gate = gating_strategy.get_gate(gate_id, gate_path)

        # dim count determines if we need a histogram, scatter, or multi-scatter
        dim_count = len(gate.dimensions)
        if dim_count == 1:
            gate_type = 'hist'
        elif dim_count == 2:
            gate_type = 'scatter'
        else:
            raise NotImplementedError("Plotting of gates with >2 dimensions is not yet supported")

        sample_to_plot = self.get_sample(sample_id)
        events, dim_idx, dim_min, dim_max, new_dims = gate.preprocess_sample_events(
            sample_to_plot,
            copy.deepcopy(gating_strategy)
        )

        # get parent gate results to display only those events
        if gate.parent is not None:
            parent_results = gating_strategy.gate_sample(sample_to_plot, gate.parent)
            is_parent_event = parent_results.get_gate_indices(gate.parent)
            is_subsample = np.zeros(sample_to_plot.event_count, dtype=bool)
            is_subsample[sample_to_plot.subsample_indices] = True
            idx_to_plot = np.logical_and(is_parent_event, is_subsample)
        else:
            idx_to_plot = sample_to_plot.subsample_indices

        if len(new_dims) > 0:
            raise NotImplementedError("Plotting of RatioDimensions is not yet supported.")

        x = events[idx_to_plot, dim_idx[0]]

        dim_labels = []

        x_index = dim_idx[0]
        x_pnn_label = sample_to_plot.pnn_labels[x_index]
        y_pnn_label = None

        if sample_to_plot.pns_labels[x_index] != '':
            dim_labels.append('%s (%s)' % (sample_to_plot.pns_labels[x_index], x_pnn_label))
        else:
            dim_labels.append(sample_to_plot.pnn_labels[x_index])

        if len(dim_idx) > 1:
            y_index = dim_idx[1]
            y_pnn_label = sample_to_plot.pnn_labels[y_index]

            if sample_to_plot.pns_labels[y_index] != '':
                dim_labels.append('%s (%s)' % (sample_to_plot.pns_labels[y_index], y_pnn_label))
            else:
                dim_labels.append(sample_to_plot.pnn_labels[y_index])

        if gate_type == 'scatter':
            y = events[idx_to_plot, dim_idx[1]]

            p = plot_utils.plot_scatter(
                x,
                y,
                dim_labels,
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                color_density=color_density
            )
        elif gate_type == 'hist':
            p = plot_utils.plot_histogram(x, dim_labels[0])
        else:
            raise NotImplementedError("Only histograms and scatter plots are supported in this version of FlowKit")

        if isinstance(gate, gates.PolygonGate):
            source, glyph = plot_utils.render_polygon(gate.vertices)
            p.add_glyph(source, glyph)
        elif isinstance(gate, gates.EllipsoidGate):
            ellipse = plot_utils.calculate_ellipse(
                gate.coordinates[0],
                gate.coordinates[1],
                gate.covariance_matrix,
                gate.distance_square
            )
            p.add_glyph(ellipse)
        elif isinstance(gate, gates.RectangleGate):
            # rectangle gates in GatingML may not actually be rectangles, as the min/max for the dimensions
            # are options. So, if any of the dim min/max values are missing it is essentially a set of ranges.

            if None in dim_min or None in dim_max or dim_count == 1:
                renderers = plot_utils.render_ranges(dim_min, dim_max)

                p.renderers.extend(renderers)
            else:
                # a true rectangle
                rect = plot_utils.render_rectangle(dim_min, dim_max)
                p.add_glyph(rect)
        elif isinstance(gate, gates.QuadrantGate):
            x_locations = []
            y_locations = []

            for div in gate.dimensions:
                if div.dimension_ref == x_pnn_label:
                    x_locations.extend(div.values)
                elif div.dimension_ref == y_pnn_label and y_pnn_label is not None:
                    y_locations.extend(div.values)

            renderers = plot_utils.render_dividers(x_locations, y_locations)
            p.renderers.extend(renderers)
        else:
            raise NotImplementedError(
                "Plotting of %s gates is not supported in this version of FlowKit" % gate.__class__
            )

        if gate_path is not None:
            full_gate_path = gate_path[1:]  # omit 'root'
            full_gate_path.append(gate_id)
            sub_title = ' > '.join(full_gate_path)

            # truncate beginning of long gate paths
            if len(sub_title) > 72:
                sub_title = '...' + sub_title[-69:]
            p.add_layout(
                Title(text=sub_title, text_font_style="italic", text_font_size="1em", align='center'),
                'above'
            )
        else:
            p.add_layout(
                Title(text=gate_id, text_font_style="italic", text_font_size="1em", align='center'),
                'above'
            )

        plot_title = "%s (%s)" % (sample_id, group_name)
        p.add_layout(
            Title(text=plot_title, text_font_size="1.1em", align='center'),
            'above'
        )

        return p

    def plot_scatter(
            self,
            sample_id,
            x_dim,
            y_dim,
            group_name='default',
            gate_id=None,
            subsample=False,
            color_density=True,
            x_min=None,
            x_max=None,
            y_min=None,
            y_max=None
    ):
        """
        Returns an interactive scatter plot for the specified channel data.

        :param sample_id: The sample ID for the FCS sample to plot
        :param x_dim:  Dimension instance to use for the x-axis data
        :param y_dim: Dimension instance to use for the y-axis data
        :param group_name: The sample group containing the sample ID (and, optionally the gate ID)
        :param gate_id: Gate ID to filter events (only events within the given gate will be plotted)
        :param subsample: Whether to use all events for plotting or just the
            sub-sampled events. Default is False (all events). Plotting
            sub-sampled events can be much faster.
        :param color_density: Whether to color the events by density, similar
            to a heat map. Default is True.
        :param x_min: Lower bound of x-axis. If None, channel's min value will
            be used with some padding to keep events off the edge of the plot.
        :param x_max: Upper bound of x-axis. If None, channel's max value will
            be used with some padding to keep events off the edge of the plot.
        :param y_min: Lower bound of y-axis. If None, channel's min value will
            be used with some padding to keep events off the edge of the plot.
        :param y_max: Upper bound of y-axis. If None, channel's max value will
            be used with some padding to keep events off the edge of the plot.
        :return: A Bokeh Figure object containing the interactive scatter plot.
        """
        sample = self.get_sample(sample_id)
        group = self._sample_group_lut[group_name]
        gating_strategy = group['samples'][sample_id]

        x_index = sample.get_channel_index(x_dim.label)
        y_index = sample.get_channel_index(y_dim.label)

        x_comp_ref = x_dim.compensation_ref
        x_xform_ref = x_dim.transformation_ref

        y_comp_ref = y_dim.compensation_ref
        y_xform_ref = y_dim.transformation_ref

        if x_comp_ref is not None and x_comp_ref != 'uncompensated':
            x_comp = gating_strategy.get_comp_matrix(x_dim.compensation_ref)
            comp_events = x_comp.apply(sample)
            x = comp_events[:, x_index]
        else:
            # not doing sub-sample here, will do later with bool AND
            x = sample.get_channel_data(x_index, source='raw', subsample=False)

        if y_comp_ref is not None and x_comp_ref != 'uncompensated':
            # this is likely unnecessary as the x & y comp should be the same,
            # but requires more conditionals to cover
            y_comp = gating_strategy.get_comp_matrix(x_dim.compensation_ref)
            comp_events = y_comp.apply(sample)
            y = comp_events[:, y_index]
        else:
            # not doing sub-sample here, will do later with bool AND
            y = sample.get_channel_data(y_index, source='raw', subsample=False)

        if x_xform_ref is not None:
            x_xform = gating_strategy.get_transform(x_xform_ref)
            x = x_xform.apply(x.reshape(-1, 1))[:, 0]
        if y_xform_ref is not None:
            y_xform = gating_strategy.get_transform(y_xform_ref)
            y = y_xform.apply(y.reshape(-1, 1))[:, 0]

        if gate_id is not None:
            gate_results = gating_strategy.gate_sample(sample, gate_id)
            is_gate_event = gate_results.get_gate_indices(gate_id)
            if subsample:
                is_subsample = np.zeros(sample.event_count, dtype=bool)
                is_subsample[sample.subsample_indices] = True
            else:
                is_subsample = np.ones(sample.event_count, dtype=bool)

            idx_to_plot = np.logical_and(is_gate_event, is_subsample)
            x = x[idx_to_plot]
            y = y[idx_to_plot]

        dim_labels = []

        if sample.pns_labels[x_index] != '':
            dim_labels.append('%s (%s)' % (sample.pns_labels[x_index], sample.pnn_labels[x_index]))
        else:
            dim_labels.append(sample.pnn_labels[x_index])

        if sample.pns_labels[y_index] != '':
            dim_labels.append('%s (%s)' % (sample.pns_labels[y_index], sample.pnn_labels[y_index]))
        else:
            dim_labels.append(sample.pnn_labels[y_index])

        p = plot_utils.plot_scatter(
            x,
            y,
            dim_labels,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            color_density=color_density
        )

        p.title = Title(text=sample.original_filename, align='center')

        return p
