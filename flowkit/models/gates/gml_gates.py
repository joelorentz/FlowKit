# noinspection PyUnresolvedReferences
from lxml import etree, objectify
from flowkit.models import gates
from flowkit import gml_utils


class GMLRectangleGate(gates.RectangleGate):
    """
    Represents a GatingML Rectangle Gate

    A RectangleGate can have one or more dimensions, and each dimension must
    specify at least one of a minimum or maximum value (or both). From the
    GatingML specification (sect. 5.1.1):

        Rectangular gates are used to express range gates (n = 1, i.e., one
        dimension), rectangle gates (n = 2, i.e., two dimensions), box regions
        (n = 3, i.e., three dimensions), and hyper-rectangular regions
        (n > 3, i.e., more than three dimensions).
    """
    def __init__(
            self,
            gate_element,
            gating_namespace,
            data_type_namespace,
            gating_strategy
    ):
        gate_id, parent_id, dimensions = gml_utils.parse_gate_element(
            gate_element,
            gating_namespace,
            data_type_namespace
        )

        super().__init__(
            gate_id,
            parent_id,
            dimensions,
            gating_strategy
        )


class GMLPolygonGate(gates.PolygonGate):
    """
    Represents a GatingML Polygon Gate

    A PolygonGate must have exactly 2 dimensions, and must specify at least
    three vertices. Polygons can have crossing boundaries, and interior regions
    are defined by the winding number method:
        https://en.wikipedia.org/wiki/Winding_number
    """
    def __init__(
            self,
            gate_element,
            gating_namespace,
            data_type_namespace,
            gating_strategy
    ):
        gate_id, parent_id, dimensions = gml_utils.parse_gate_element(
            gate_element,
            gating_namespace,
            data_type_namespace
        )

        vert_els = gate_element.findall(
            '%s:vertex' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        vertices = []

        for vert_el in vert_els:
            vert = gml_utils.parse_vertex_element(vert_el, gating_namespace, data_type_namespace)
            vertices.append(vert)

        super().__init__(
            gate_id,
            parent_id,
            dimensions,
            vertices,
            gating_strategy
        )


class GMLEllipsoidGate(gates.EllipsoidGate):
    """
    Represents a GatingML Ellipsoid Gate

    An EllipsoidGate must have at least 2 dimensions, and must specify a mean
    value (center of the ellipsoid), a covariance matrix, and a distance
    square (the square of the Mahalanobis distance).
    """
    def __init__(
            self,
            gate_element,
            gating_namespace,
            data_type_namespace,
            gating_strategy
    ):
        gate_id, parent_id, dimensions = gml_utils.parse_gate_element(
            gate_element,
            gating_namespace,
            data_type_namespace
        )

        # First, we'll get the center of the ellipse, contained in
        # a 'mean' element, that holds 2 'coordinate' elements
        mean_el = gate_element.find(
            '%s:mean' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        coordinates = []

        coord_els = mean_el.findall(
            '%s:coordinate' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        if len(coord_els) == 1:
            raise ValueError(
                'Ellipsoids must have at least 2 dimensions (line %d)' % gate_element.sourceline
            )

        for coord_el in coord_els:
            value = gml_utils.find_attribute_value(coord_el, data_type_namespace, 'value')
            if value is None:
                raise ValueError(
                    'A coordinate must have only 1 value (line %d)' % coord_el.sourceline
                )

            coordinates.append(float(value))

        # Next, we'll parse the covariance matrix, containing 2 'row'
        # elements, each containing 2 'entry' elements w/ value attributes
        covariance_el = gate_element.find(
            '%s:covarianceMatrix' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        covariance_matrix = []

        covariance_row_els = covariance_el.findall(
            '%s:row' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        for row_el in covariance_row_els:
            row_entry_els = row_el.findall(
                '%s:entry' % gating_namespace,
                namespaces=gate_element.nsmap
            )

            entry_values = []
            for entry_el in row_entry_els:
                value = gml_utils.find_attribute_value(entry_el, data_type_namespace, 'value')
                entry_values.append(float(value))

            if len(entry_values) != len(coordinates):
                raise ValueError(
                    'Covariance row entry value count must match # of dimensions (line %d)' % row_el.sourceline
                )

            covariance_matrix.append(entry_values)

        # Finally, get the distance square, which is a simple element w/
        # a single value attribute
        distance_square_el = gate_element.find(
            '%s:distanceSquare' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        dist_square_value = gml_utils.find_attribute_value(distance_square_el, data_type_namespace, 'value')
        distance_square = float(dist_square_value)

        super().__init__(
            gate_id,
            parent_id,
            dimensions,
            coordinates,
            covariance_matrix,
            distance_square,
            gating_strategy
        )


class GMLQuadrantGate(gates.QuadrantGate):
    """
    Represents a GatingML Quadrant Gate

    A QuadrantGate must have at least 1 divider, and must specify the labels
    of the resulting quadrants the dividers produce. Quadrant gates are
    different from other gate types in that they are actually a collection of
    gates (quadrants), though even the term quadrant is misleading as they can
    divide a plane into more than 4 sections.

    Note: Only specific quadrants may be referenced as parent gates or as a
    component of a Boolean gate. If a QuadrantGate has a parent, then the
    parent gate is applicable to all quadrants in the QuadrantGate.
    """
    def __init__(
            self,
            gate_element,
            gating_namespace,
            data_type_namespace,
            gating_strategy
    ):
        gate_id, parent_id, dividers = gml_utils.parse_gate_element(
            gate_element,
            gating_namespace,
            data_type_namespace
        )

        # First, we'll check dimension count
        if len(dividers) < 1:
            raise ValueError(
                'Quadrant gates must have at least 1 divider (line %d)' % gate_element.sourceline
            )

        # Next, we'll parse the Quadrant elements, each containing an
        # id attribute, and 1 or more 'position' elements. Each position
        # element has a 'divider-ref' and 'location' attribute.
        quadrant_els = gate_element.findall(
            '%s:Quadrant' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        quadrants = {}

        for quadrant_el in quadrant_els:
            quad_id = gml_utils.find_attribute_value(quadrant_el, gating_namespace, 'id')
            quadrants[quad_id] = []

            position_els = quadrant_el.findall(
                '%s:position' % gating_namespace,
                namespaces=gate_element.nsmap
            )

            for pos_el in position_els:
                divider_ref = gml_utils.find_attribute_value(pos_el, gating_namespace, 'divider_ref')
                location = gml_utils.find_attribute_value(pos_el, gating_namespace, 'location')

                divider = divider_ref
                location = float(location)
                q_min = None
                q_max = None
                dim_label = None

                for div in dividers:
                    if div.id != divider:
                        continue
                    else:
                        dim_label = div.dimension_ref

                    for v in sorted(div.values):
                        if v > location:
                            q_max = v

                            # once we have a max value, no need to
                            break
                        elif v <= location:
                            q_min = v

                if dim_label is None:
                    raise ValueError(
                        'Quadrant must define a divider reference (line %d)' % pos_el.sourceline
                    )

                quadrants[quad_id].append(
                    {
                        'divider': divider,
                        'dimension': dim_label,
                        'location': location,
                        'min': q_min,
                        'max': q_max
                    }
                )

        super().__init__(
            gate_id,
            parent_id,
            dividers,
            quadrants,
            gating_strategy
        )


class GMLBooleanGate(gates.BooleanGate):
    """
    Represents a GatingML Boolean Gate

    A BooleanGate performs the boolean operations AND, OR, or NOT on one or
    more other gates. Note, the boolean operation XOR is not supported in the
    GatingML specification but can be implemented using a combination of the
    supported operations.
    """
    def __init__(
            self,
            gate_element,
            gating_namespace,
            data_type_namespace,
            gating_strategy
    ):
        gate_id, parent_id, dimensions = gml_utils.parse_gate_element(
            gate_element,
            gating_namespace,
            data_type_namespace
        )
        
        # boolean gates do not mix multiple operations, so there should be only
        # one of the following: 'and', 'or', or 'not'
        and_els = gate_element.findall(
            '%s:and' % gating_namespace,
            namespaces=gate_element.nsmap
        )
        or_els = gate_element.findall(
            '%s:or' % gating_namespace,
            namespaces=gate_element.nsmap
        )
        not_els = gate_element.findall(
            '%s:not' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        if len(and_els) > 0:
            bool_type = 'and'
            bool_op_el = and_els[0]
        elif len(or_els) > 0:
            bool_type = 'or'
            bool_op_el = or_els[0]
        elif len(not_els) > 0:
            bool_type = 'not'
            bool_op_el = not_els[0]
        else:
            raise ValueError(
                "Boolean gate must specify one of 'and', 'or', or 'not' (line %d)" % gate_element.sourceline
            )

        gate_ref_els = bool_op_el.findall(
            '%s:gateReference' % gating_namespace,
            namespaces=gate_element.nsmap
        )

        gate_refs = []

        for gate_ref_el in gate_ref_els:
            gate_ref = gml_utils.find_attribute_value(gate_ref_el, gating_namespace, 'ref')
            if gate_ref is None:
                raise ValueError(
                    "Boolean gate reference must specify a 'ref' attribute (line %d)" % gate_ref_el.sourceline
                )

            use_complement = gml_utils.find_attribute_value(gate_ref_el, gating_namespace, 'use-as-complement')
            if use_complement is not None:
                use_complement = use_complement == 'true'
            else:
                use_complement = False

            gate_refs.append(
                {
                    'ref': gate_ref,
                    'complement': use_complement
                }
            )

        super().__init__(
            gate_id,
            parent_id,
            dimensions,
            bool_type,
            gate_refs,
            gating_strategy
        )