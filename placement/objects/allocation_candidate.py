#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import copy
import itertools

import os_traits
from oslo_log import log as logging
from oslo_utils import encodeutils
import six
import sqlalchemy as sa
from sqlalchemy import sql

from placement.db.sqlalchemy import models
from placement import db_api
from placement import exception
from placement.objects import research_context as res_ctx
from placement.objects import resource_provider as rp_obj
from placement.objects import trait as trait_obj
from placement import resource_class_cache as rc_cache


_ALLOC_TBL = models.Allocation.__table__
_INV_TBL = models.Inventory.__table__
_RP_TBL = models.ResourceProvider.__table__

LOG = logging.getLogger(__name__)


class AllocationCandidates(object):
    """The AllocationCandidates object is a collection of possible allocations
    that match some request for resources, along with some summary information
    about the resource providers involved in these allocation candidates.
    """

    def __init__(self, allocation_requests=None, provider_summaries=None):
        # A collection of allocation possibilities that can be attempted by the
        # caller that would, at the time of calling, meet the requested
        # resource constraints
        self.allocation_requests = allocation_requests
        # Information about usage and inventory that relate to any provider
        # contained in any of the AllocationRequest objects in the
        # allocation_requests field
        self.provider_summaries = provider_summaries

    @classmethod
    def get_by_requests(cls, context, groups, rqparams, nested_aware=True):
        """Returns an AllocationCandidates object containing all resource
        providers matching a set of supplied resource constraints, with a set
        of allocation requests constructed from that list of resource
        providers. If CONF.placement.randomize_allocation_candidates (on
        contex.config) is True (default is False) then the order of the
        allocation requests will be randomized.

        :param context: placement.context.RequestContext object.
        :param groups: Dict, keyed by suffix, of placement.lib.RequestGroup
        :param rqparams: A RequestWideParams.
        :param nested_aware: If False, we are blind to nested architecture and
                             can't pick resources from multiple providers even
                             if they come from the same tree.
        :return: An instance of AllocationCandidates with allocation_requests
                 and provider_summaries satisfying `requests`, limited
                 according to `limit`.
        """
        try:
            alloc_reqs, provider_summaries = cls._get_by_requests(
                context, groups, rqparams, nested_aware=nested_aware)
        except exception.ResourceProviderNotFound:
            alloc_reqs, provider_summaries = [], []
        return cls(
            allocation_requests=alloc_reqs,
            provider_summaries=provider_summaries,
        )

    @staticmethod
    def _get_by_one_request(rg_ctx, rw_ctx):
        """Get allocation candidates for one RequestGroup.

        Must be called from within an placement_context_manager.reader
        (or writer) context.

        :param rg_ctx: RequestGroupSearchContext.
        :param rw_ctx: RequestWideSearchContext.
        """
        if not rg_ctx.use_same_provider and (
                rg_ctx.exists_sharing or rg_ctx.exists_nested):
            # TODO(jaypipes): The check/callout to handle trees goes here.
            # Build a dict, keyed by resource class internal ID, of lists of
            # internal IDs of resource providers that share some inventory for
            # each resource class requested.
            # If there aren't any providers that have any of the
            # required traits, just exit early...
            if rg_ctx.required_trait_map:
                # TODO(cdent): Now that there is also a forbidden_trait_map
                # it should be possible to further optimize this attempt at
                # a quick return, but we leave that to future patches for
                # now.
                trait_rps = res_ctx.get_provider_ids_having_any_trait(
                    rg_ctx.context, rg_ctx.required_trait_map)
                if not trait_rps:
                    return [], []
            rp_candidates = res_ctx.get_trees_matching_all(rg_ctx, rw_ctx)
            return _alloc_candidates_multiple_providers(rg_ctx, rp_candidates)

        # Either we are processing a single-RP request group, or there are no
        # sharing providers that (help) satisfy the request.  Get a list of
        # tuples of (internal provider ID, root provider ID) that have ALL
        # the requested resources and more efficiently construct the
        # allocation requests.
        rp_tuples = res_ctx.get_provider_ids_matching(rg_ctx)
        return _alloc_candidates_single_provider(rg_ctx, rw_ctx, rp_tuples)

    @classmethod
    @db_api.placement_context_manager.reader
    def _get_by_requests(cls, context, groups, rqparams, nested_aware=True):
        rw_ctx = res_ctx.RequestWideSearchContext(
            context, rqparams, nested_aware)
        sharing = res_ctx.get_sharing_providers(context)
        # TODO(efried): If we ran anchors_for_sharing_providers here, we could
        #  narrow to only sharing providers associated with our filtered trees.
        #  Unclear whether this would be cheaper than waiting until we've
        #  filtered sharing providers for other things (like resources).

        candidates = {}
        for suffix, group in groups.items():
            rg_ctx = res_ctx.RequestGroupSearchContext(
                context, group, rw_ctx.has_trees, sharing, suffix)

            alloc_reqs, summaries = cls._get_by_one_request(rg_ctx, rw_ctx)
            LOG.debug("%s (suffix '%s') returned %d matches",
                      str(group), str(suffix), len(alloc_reqs))
            if not alloc_reqs:
                # Shortcut: If any one group resulted in no candidates, the
                # whole operation is shot.
                return [], []
            # Mark each allocation request according to whether its
            # corresponding RequestGroup required it to be restricted to a
            # single provider.  We'll need this later to evaluate group_policy.
            for areq in alloc_reqs:
                areq.use_same_provider = group.use_same_provider
            candidates[suffix] = alloc_reqs, summaries

        # At this point, each (alloc_requests, summary_obj) in `candidates` is
        # independent of the others. We need to fold them together such that
        # each allocation request satisfies *all* the incoming `requests`.  The
        # `candidates` dict is guaranteed to contain entries for all suffixes,
        # or we would have short-circuited above.
        alloc_request_objs, summary_objs = _merge_candidates(
            candidates, rw_ctx)

        alloc_request_objs, summary_objs = rw_ctx.exclude_nested_providers(
            alloc_request_objs, summary_objs)

        return rw_ctx.limit_results(alloc_request_objs, summary_objs)


class AllocationRequest(object):

    def __init__(self, anchor_root_provider_uuid=None,
                 use_same_provider=None, resource_requests=None,
                 mappings=None):
        # UUID of (the root of the tree including) the non-sharing resource
        # provider associated with this AllocationRequest. Internal use only,
        # not included when the object is serialized for output.
        self.anchor_root_provider_uuid = anchor_root_provider_uuid
        # Whether all AllocationRequestResources in this AllocationRequest are
        # required to be satisfied by the same provider (based on the
        # corresponding RequestGroup's use_same_provider attribute). Internal
        # use only, not included when the object is serialized for output.
        self.use_same_provider = use_same_provider
        self.resource_requests = resource_requests or []
        # mappings will be presented as a dict during output, so ensure we have
        # a reasonable default here, despite mappings always being set.
        self.mappings = mappings or dict()

    def __repr__(self):
        anchor = (self.anchor_root_provider_uuid[-8:]
                  if self.anchor_root_provider_uuid else '<?>')
        usp = (self.use_same_provider
               if self.use_same_provider is not None else '<?>')
        repr_str = ('%s(anchor=...%s, same_provider=%s, '
                    'resource_requests=[%s])' %
                    (self.__class__.__name__, anchor, usp,
                     ', '.join([str(arr) for arr in self.resource_requests])))
        if six.PY2:
            repr_str = encodeutils.safe_encode(repr_str, incoming='utf-8')
        return repr_str

    def __eq__(self, other):
        return (set(self.resource_requests) == set(other.resource_requests)
                and self.mappings == other.mappings)

    def __hash__(self):
        # We need a stable sort order on the resource requests to get an
        # accurate hash. To avoid needing to update the method everytime
        # the structure of an AllocationRequestResource changes, we can
        # sort on the hash of each request resource.
        sorted_rr = sorted(self.resource_requests, key=lambda x: hash(x))
        return hash(tuple(sorted_rr))


class AllocationRequestResource(object):

    def __init__(self, resource_provider=None, resource_class=None,
                 amount=None):
        self.resource_provider = resource_provider
        self.resource_class = resource_class
        self.amount = amount

    def __eq__(self, other):
        return ((self.resource_provider.id == other.resource_provider.id) and
                (self.resource_class == other.resource_class) and
                (self.amount == other.amount))

    def __hash__(self):
        return hash((self.resource_provider.id,
                     self.resource_class,
                     self.amount))


class ProviderSummary(object):

    def __init__(self, resource_provider=None, resources=None, traits=None):
        self.resource_provider = resource_provider
        self.resources = resources or []
        self.traits = traits or []


class ProviderSummaryResource(object):

    def __init__(self, resource_class=None, capacity=None, used=None,
                 max_unit=None):
        self.resource_class = resource_class
        self.capacity = capacity
        self.used = used
        # Internal use only; not included when the object is serialized for
        # output.
        self.max_unit = max_unit


def _alloc_candidates_multiple_providers(rg_ctx, rp_candidates):
    """Returns a tuple of (allocation requests, provider summaries) for a
    supplied set of requested resource amounts and tuples of
    (rp_id, root_id, rc_id). The supplied resource provider trees have
    capacity to satisfy ALL of the resources in the requested resources as
    well as ALL required traits that were requested by the user.

    This is a code path to get results for a RequestGroup with
    use_same_provider=False. In this scenario, we are able to use multiple
    providers within the same provider tree including sharing providers to
    satisfy different resources involved in a single request group.

    :param rg_ctx: RequestGroupSearchContext.
    :param rp_candidates: RPCandidates object representing the providers
                          that satisfy the request for resources.
    """
    if not rp_candidates:
        return [], []

    # Get all the root resource provider IDs. We should include the first
    # values of rp_tuples because while sharing providers are root providers,
    # they have their "anchor" providers for the second value.
    root_ids = rp_candidates.all_rps

    # Grab usage summaries for each provider in the trees
    usages = _get_usages_by_provider_tree(rg_ctx.context, root_ids)

    # Get a dict, keyed by resource provider internal ID, of trait string names
    # that provider has associated with it
    prov_traits = trait_obj.get_traits_by_provider_tree(
        rg_ctx.context, root_ids)

    # Get a dict, keyed by resource provider internal ID, of ProviderSummary
    # objects for all providers
    summaries = _build_provider_summaries(rg_ctx.context, usages, prov_traits)

    # Get a dict, keyed by root provider internal ID, of a dict, keyed by
    # resource class internal ID, of lists of AllocationRequestResource objects
    tree_dict = collections.defaultdict(lambda: collections.defaultdict(list))

    for rp in rp_candidates.rps_info:
        rp_summary = summaries[rp.id]
        tree_dict[rp.root_id][rp.rc_id].append(
            AllocationRequestResource(
                resource_provider=rp_summary.resource_provider,
                resource_class=rc_cache.RC_CACHE.string_from_id(rp.rc_id),
                amount=rg_ctx.resources[rp.rc_id]))

    # Next, build up a set of allocation requests. These allocation requests
    # are AllocationRequest objects, containing resource provider UUIDs,
    # resource class names and amounts to consume from that resource provider
    alloc_requests = set()

    # Let's look into each tree
    for root_id, alloc_dict in tree_dict.items():
        # Get request_groups, which is a list of lists of
        # AllocationRequestResource(ARR) per requested resource class(rc).
        # For example, if we have the alloc_dict:
        # {rc1_id: [ARR(rc1, rp1), ARR(rc1, rp2)],
        #  rc2_id: [ARR(rc2, rp1), ARR(rc2, rp2)],
        #  rc3_id: [ARR(rc3, rp1)]}
        # then the request_groups would be something like
        # [[ARR(rc1, rp1), ARR(rc1, rp2)],
        #  [ARR(rc2, rp1), ARR(rc2, rp2)],
        #  [ARR(rc3, rp1)]]
        # , which should be ordered by the resource class id.
        request_groups = [val for key, val in sorted(alloc_dict.items())]

        root_summary = summaries[root_id]
        root_uuid = root_summary.resource_provider.uuid
        root_alloc_reqs = set()

        # Using itertools.product, we get all the combinations of resource
        # providers in a tree.
        # For example, the sample in the comment above becomes:
        # [(ARR(rc1, ss1), ARR(rc2, ss1), ARR(rc3, ss1)),
        #  (ARR(rc1, ss1), ARR(rc2, ss2), ARR(rc3, ss1)),
        #  (ARR(rc1, ss2), ARR(rc2, ss1), ARR(rc3, ss1)),
        #  (ARR(rc1, ss2), ARR(rc2, ss2), ARR(rc3, ss1))]
        for res_requests in itertools.product(*request_groups):
            if not _check_traits_for_alloc_request(
                    res_requests, summaries,
                    rg_ctx.required_trait_map,
                    rg_ctx.forbidden_trait_map):
                # This combination doesn't satisfy trait constraints
                continue

            mappings = collections.defaultdict(set)
            for rr in res_requests:
                mappings[rg_ctx.suffix].add(rr.resource_provider.uuid)
            alloc_req = AllocationRequest(resource_requests=list(res_requests),
                                          anchor_root_provider_uuid=root_uuid,
                                          mappings=mappings)
            root_alloc_reqs.add(alloc_req)
        alloc_requests |= root_alloc_reqs
    return list(alloc_requests), list(summaries.values())


def _alloc_candidates_single_provider(rg_ctx, rw_ctx, rp_tuples):
    """Returns a tuple of (allocation requests, provider summaries) for a
    supplied set of requested resource amounts and resource providers. The
    supplied resource providers have capacity to satisfy ALL of the resources
    in the requested resources as well as ALL required traits that were
    requested by the user.

    This is used in two circumstances:
    - To get results for a RequestGroup with use_same_provider=True.
    - As an optimization when no sharing providers satisfy any of the requested
      resources, and nested providers are not in play.
    In these scenarios, we can more efficiently build the list of
    AllocationRequest and ProviderSummary objects due to not having to
    determine requests across multiple providers.

    :param rg_ctx: RequestGroupSearchContext
    :param rw_ctx: RequestWideSearchContext
    :param rp_tuples: List of two-tuples of (provider ID, root provider ID)s
                      for providers that matched the requested resources
    """
    if not rp_tuples:
        return [], []

    # Get all root resource provider IDs.
    root_ids = set(p[1] for p in rp_tuples)

    # Grab usage summaries for each provider
    usages = _get_usages_by_provider_tree(rg_ctx.context, root_ids)

    # Get a dict, keyed by resource provider internal ID, of trait string names
    # that provider has associated with it
    prov_traits = trait_obj.get_traits_by_provider_tree(
        rg_ctx.context, root_ids)

    # Get a dict, keyed by resource provider internal ID, of ProviderSummary
    # objects for all providers
    summaries = _build_provider_summaries(rg_ctx.context, usages, prov_traits)

    # Next, build up a list of allocation requests. These allocation requests
    # are AllocationRequest objects, containing resource provider UUIDs,
    # resource class names and amounts to consume from that resource provider
    alloc_requests = []
    for rp_id, root_id in rp_tuples:
        rp_summary = summaries[rp_id]
        req_obj = _allocation_request_for_provider(
            rg_ctx.resources, rp_summary.resource_provider,
            suffix=rg_ctx.suffix)
        # Exclude this if its anchor (which is its root) isn't in our
        # prefiltered list of anchors
        if rw_ctx.in_filtered_anchors(root_id):
            alloc_requests.append(req_obj)
        # If this is a sharing provider, we have to include an extra
        # AllocationRequest for every possible anchor.
        traits = rp_summary.traits
        if os_traits.MISC_SHARES_VIA_AGGREGATE in traits:
            anchors = res_ctx.anchors_for_sharing_providers(
                rg_ctx.context, [rp_summary.resource_provider.id])
            for anchor in anchors:
                # We already added self
                if anchor.anchor_id == root_id:
                    continue
                # Only include if anchor is viable
                if not rw_ctx.in_filtered_anchors(anchor.anchor_id):
                    continue
                req_obj = copy.copy(req_obj)
                req_obj.anchor_root_provider_uuid = anchor.anchor_uuid
                alloc_requests.append(req_obj)
    return alloc_requests, list(summaries.values())


def _allocation_request_for_provider(requested_resources, provider, suffix):
    """Returns an AllocationRequest object containing AllocationRequestResource
    objects for each resource class in the supplied requested resources dict.

    :param requested_resources: dict, keyed by resource class ID, of amounts
                                being requested for that resource class
    :param provider: ResourceProvider object representing the provider of the
                     resources.
    :param suffix: The suffix of the RequestGroup these resources are
                   satisfying.
    """
    resource_requests = [
        AllocationRequestResource(
            resource_provider=provider,
            resource_class=rc_cache.RC_CACHE.string_from_id(rc_id),
            amount=amount
        ) for rc_id, amount in requested_resources.items()
    ]
    # NOTE(efried): This method only produces an AllocationRequest with its
    # anchor in its own tree.  If the provider is a sharing provider, the
    # caller needs to identify the other anchors with which it might be
    # associated.
    # NOTE(tetsuro): The AllocationRequest has empty resource_requests for a
    # resourceless request. Still, it has the rp uuid in the mappings field.
    mappings = {suffix: set([provider.uuid])}
    return AllocationRequest(
        resource_requests=resource_requests,
        anchor_root_provider_uuid=provider.root_provider_uuid,
        mappings=mappings)


def _build_provider_summaries(context, usages, prov_traits):
    """Given a list of dicts of usage information and a map of providers to
    their associated string traits, returns a dict, keyed by resource provider
    ID, of ProviderSummary objects.

    :param context: placement.context.RequestContext object
    :param usages: A list of dicts with the following format:

        {
            'resource_provider_id': <internal resource provider ID>,
            'resource_provider_uuid': <UUID>,
            'resource_class_id': <internal resource class ID>,
            'total': integer,
            'reserved': integer,
            'allocation_ratio': float,
        }
    :param prov_traits: A dict, keyed by internal resource provider ID, of
                        string trait names associated with that provider
    """
    # Before we go creating provider summary objects, first grab all the
    # provider information (including root, parent and UUID information) for
    # all providers involved in our operation
    rp_ids = set(usage['resource_provider_id'] for usage in usages)
    provider_ids = res_ctx.provider_ids_from_rp_ids(context, rp_ids)

    # Build up a dict, keyed by internal resource provider ID, of
    # ProviderSummary objects containing one or more ProviderSummaryResource
    # objects representing the resources the provider has inventory for.
    summaries = {}
    for usage in usages:
        rp_id = usage['resource_provider_id']
        summary = summaries.get(rp_id)
        if not summary:
            pids = provider_ids[rp_id]
            summary = ProviderSummary(
                resource_provider=rp_obj.ResourceProvider(
                    context, id=pids.id, uuid=pids.uuid,
                    root_provider_uuid=pids.root_uuid,
                    parent_provider_uuid=pids.parent_uuid),
                resources=[],
            )
            summaries[rp_id] = summary

        summary.traits = prov_traits[rp_id]

        rc_id = usage['resource_class_id']
        if rc_id is None:
            # NOTE(tetsuro): This provider doesn't have any inventory itself.
            # But we include this provider in summaries since another
            # provider in the same tree will be in the "allocation_request".
            # Let's skip the following and leave "ProviderSummary.resources"
            # field empty.
            continue
        # NOTE(jaypipes): usage['used'] may be None due to the LEFT JOIN of
        # the usages subquery, so we coerce NULL values to 0 here. It may
        # also be a Decimal, as that's the type that mysql tends to return
        # when func.sum is used in a query. We need an int, otherwise later
        # JSON serialization will not work.
        used = int(usage['used'] or 0)
        allocation_ratio = usage['allocation_ratio']
        cap = int((usage['total'] - usage['reserved']) * allocation_ratio)
        rc_name = rc_cache.RC_CACHE.string_from_id(rc_id)
        rpsr = ProviderSummaryResource(
            resource_class=rc_name,
            capacity=cap,
            used=used,
            max_unit=usage['max_unit'],
        )
        summary.resources.append(rpsr)
    return summaries


def _check_traits_for_alloc_request(res_requests, summaries, required_traits,
                                    forbidden_traits):
    """Given a list of AllocationRequestResource objects, check if that
    combination can provide trait constraints. If it can, returns all
    resource provider internal IDs in play, else return an empty list.

    TODO(tetsuro): For optimization, we should move this logic to SQL in
                   res_ctx.get_trees_matching_all().

    :param res_requests: a list of AllocationRequestResource objects that have
                         resource providers to be checked if they collectively
                         satisfy trait constraints in the required_traits and
                         forbidden_traits parameters.
    :param summaries: dict, keyed by resource provider id, of ProviderSummary
                      objects containing usage and trait information for
                      resource providers involved in the overall request
    :param required_traits: A map, keyed by trait string name, of required
                            trait internal IDs that each *allocation request's
                            set of providers* must *collectively* have
                            associated with them
    :param forbidden_traits: A map, keyed by trait string name, of trait
                             internal IDs that a resource provider must
                             not have.
    """
    all_prov_ids = []
    all_traits = set()
    for res_req in res_requests:
        rp_id = res_req.resource_provider.id
        rp_summary = summaries[rp_id]
        rp_traits = set(rp_summary.traits)

        # Check if there are forbidden_traits
        conflict_traits = set(forbidden_traits) & set(rp_traits)
        if conflict_traits:
            LOG.debug('Excluding resource provider %s, it has '
                      'forbidden traits: (%s).',
                      rp_id, ', '.join(conflict_traits))
            return []

        all_prov_ids.append(rp_id)
        all_traits |= rp_traits

    # Check if there are missing traits
    missing_traits = set(required_traits) - all_traits
    if missing_traits:
        LOG.debug('Excluding a set of allocation candidate %s : '
                  'missing traits %s are not satisfied.',
                  all_prov_ids, ','.join(missing_traits))
        return []

    return all_prov_ids


def _consolidate_allocation_requests(areqs):
    """Consolidates a list of AllocationRequest into one.

    :param areqs: A list containing one AllocationRequest for each input
            RequestGroup.  This may mean that multiple resource_requests
            contain resource amounts of the same class from the same provider.
    :return: A single consolidated AllocationRequest, containing no
            resource_requests with duplicated (resource_provider,
            resource_class).
    """
    # Construct a dict, keyed by resource provider UUID + resource class, of
    # AllocationRequestResource, consolidating as we go.
    arrs_by_rp_rc = {}
    # areqs must have at least one element.  Save the anchor to populate the
    # returned AllocationRequest.
    anchor_rp_uuid = areqs[0].anchor_root_provider_uuid
    mappings = collections.defaultdict(set)
    for areq in areqs:
        # Sanity check: the anchor should be the same for every areq
        if anchor_rp_uuid != areq.anchor_root_provider_uuid:
            # This should never happen.  If it does, it's a dev bug.
            raise ValueError(
                "Expected every AllocationRequest in "
                "`_consolidate_allocation_requests` to have the same "
                "anchor!")
        for arr in areq.resource_requests:
            key = _rp_rc_key(arr.resource_provider, arr.resource_class)
            if key not in arrs_by_rp_rc:
                arrs_by_rp_rc[key] = copy.copy(arr)
            else:
                arrs_by_rp_rc[key].amount += arr.amount
        for suffix, providers in areq.mappings.items():
            mappings[suffix].update(providers)
    return AllocationRequest(
        resource_requests=list(arrs_by_rp_rc.values()),
        anchor_root_provider_uuid=anchor_rp_uuid,
        mappings=mappings)


@db_api.placement_context_manager.reader
def _get_usages_by_provider_tree(ctx, root_ids):
    """Returns a row iterator of usage records grouped by provider ID
    for all resource providers in all trees indicated in the ``root_ids``.
    """
    # We build up a SQL expression that looks like this:
    # SELECT
    #   rp.id as resource_provider_id
    # , rp.uuid as resource_provider_uuid
    # , inv.resource_class_id
    # , inv.total
    # , inv.reserved
    # , inv.allocation_ratio
    # , inv.max_unit
    # , usage.used
    # FROM resource_providers AS rp
    # LEFT JOIN inventories AS inv
    #  ON rp.id = inv.resource_provider_id
    # LEFT JOIN (
    #   SELECT resource_provider_id, resource_class_id, SUM(used) as used
    #   FROM allocations
    #   JOIN resource_providers
    #     ON allocations.resource_provider_id = resource_providers.id
    #     AND (resource_providers.root_provider_id IN($root_ids)
    #          OR resource_providers.id IN($root_ids))
    #   GROUP BY resource_provider_id, resource_class_id
    # )
    # AS usage
    #   ON inv.resource_provider_id = usage.resource_provider_id
    #   AND inv.resource_class_id = usage.resource_class_id
    # WHERE rp.root_provider_id IN ($root_ids)
    rpt = sa.alias(_RP_TBL, name="rp")
    inv = sa.alias(_INV_TBL, name="inv")
    # Build our derived table (subquery in the FROM clause) that sums used
    # amounts for resource provider and resource class
    derived_alloc_to_rp = sa.join(
        _ALLOC_TBL, _RP_TBL,
        sa.and_(_ALLOC_TBL.c.resource_provider_id == _RP_TBL.c.id,
                _RP_TBL.c.root_provider_id.in_(root_ids))
    )
    usage = sa.alias(
        sa.select([
            _ALLOC_TBL.c.resource_provider_id,
            _ALLOC_TBL.c.resource_class_id,
            sql.func.sum(_ALLOC_TBL.c.used).label('used'),
        ]).select_from(derived_alloc_to_rp).group_by(
            _ALLOC_TBL.c.resource_provider_id,
            _ALLOC_TBL.c.resource_class_id
        ),
        name='usage')
    # Build a join between the resource providers and inventories table
    rpt_inv_join = sa.outerjoin(rpt, inv,
                                rpt.c.id == inv.c.resource_provider_id)
    # And then join to the derived table of usages
    usage_join = sa.outerjoin(
        rpt_inv_join,
        usage,
        sa.and_(
            usage.c.resource_provider_id == inv.c.resource_provider_id,
            usage.c.resource_class_id == inv.c.resource_class_id,
        ),
    )
    query = sa.select([
        rpt.c.id.label("resource_provider_id"),
        rpt.c.uuid.label("resource_provider_uuid"),
        inv.c.resource_class_id,
        inv.c.total,
        inv.c.reserved,
        inv.c.allocation_ratio,
        inv.c.max_unit,
        usage.c.used,
    ]).select_from(usage_join).where(
        rpt.c.root_provider_id.in_(root_ids)
    )
    return ctx.session.execute(query).fetchall()


def _exceeds_capacity(areq, psum_res_by_rp_rc):
    """Checks a (consolidated) AllocationRequest against the provider summaries
    to ensure that it does not exceed capacity.

    Exceeding capacity can mean the total amount (already used plus this
    allocation) exceeds the total inventory amount; or this allocation exceeds
    the max_unit in the inventory record.

    :param areq: An AllocationRequest produced by the
            `_consolidate_allocation_requests` method.
    :param psum_res_by_rp_rc: A dict, keyed by provider + resource class via
            _rp_rc_key, of ProviderSummaryResource.
    :return: True if areq exceeds capacity; False otherwise.
    """
    for arr in areq.resource_requests:
        key = _rp_rc_key(arr.resource_provider, arr.resource_class)
        psum_res = psum_res_by_rp_rc[key]
        if psum_res.used + arr.amount > psum_res.capacity:
            LOG.debug('Excluding the following AllocationRequest because used '
                      '(%d) + amount (%d) > capacity (%d) for resource class '
                      '%s: %s',
                      psum_res.used, arr.amount, psum_res.capacity,
                      arr.resource_class, str(areq))
            return True
        if arr.amount > psum_res.max_unit:
            LOG.debug('Excluding the following AllocationRequest because '
                      'amount (%d) > max_unit (%d) for resource class %s: %s',
                      arr.amount, psum_res.max_unit, arr.resource_class,
                      str(areq))
            return True
    return False


# TODO(efried): Move _merge_candidates to rw_ctx?
def _merge_candidates(candidates, rw_ctx):
    """Given a dict, keyed by RequestGroup suffix, of tuples of
    (allocation_requests, provider_summaries), produce a single tuple of
    (allocation_requests, provider_summaries) that appropriately incorporates
    the elements from each.

    Each (alloc_reqs, prov_sums) in `candidates` satisfies one RequestGroup.
    This method creates a list of alloc_reqs, *each* of which satisfies *all*
    of the RequestGroups.

    For that merged list of alloc_reqs, a corresponding provider_summaries is
    produced.

    :param candidates: A dict, keyed by suffix string or '', of tuples of
            (allocation_requests, provider_summaries) to be merged.
    :param rw_ctx: RequestWideSearchContext.
    :return: A tuple of (allocation_requests, provider_summaries).
    """
    # Build a dict, keyed by anchor root provider UUID, of dicts, keyed by
    # suffix, of nonempty lists of AllocationRequest.  Each inner dict must
    # possess all of the suffix keys to be viable (i.e. contains at least
    # one AllocationRequest per RequestGroup).
    #
    # areq_lists_by_anchor =
    #   { anchor_root_provider_uuid: {
    #         '': [AllocationRequest, ...],   \  This dict must contain
    #         '1': [AllocationRequest, ...],   \ exactly one nonempty list per
    #         ...                              / suffix to be viable. That
    #         '42': [AllocationRequest, ...], /  filtering is done later.
    #     },
    #     ...
    #   }
    areq_lists_by_anchor = collections.defaultdict(
        lambda: collections.defaultdict(list))
    # Save off all the provider summaries lists - we'll use 'em later.
    all_psums = []
    # Construct a dict, keyed by resource provider + resource class, of
    # ProviderSummaryResource.  This will be used to do a final capacity
    # check/filter on each merged AllocationRequest.
    psum_res_by_rp_rc = {}
    # A dict of parent uuids keyed by rp uuids
    parent_uuid_by_rp_uuid = {}
    for suffix, (areqs, psums) in candidates.items():
        for areq in areqs:
            anchor = areq.anchor_root_provider_uuid
            areq_lists_by_anchor[anchor][suffix].append(areq)
        for psum in psums:
            all_psums.append(psum)
            parent_uuid_by_rp_uuid[psum.resource_provider.uuid] = (
                psum.resource_provider.parent_provider_uuid)
            for psum_res in psum.resources:
                key = _rp_rc_key(
                    psum.resource_provider, psum_res.resource_class)
                psum_res_by_rp_rc[key] = psum_res

    # Create all combinations picking one AllocationRequest from each list
    # for each anchor.
    areqs = set()
    all_suffixes = set(candidates)
    num_granular_groups = len(all_suffixes - set(['']))
    for areq_lists_by_suffix in areq_lists_by_anchor.values():
        # Filter out any entries that don't have allocation requests for
        # *all* suffixes (i.e. all RequestGroups)
        if set(areq_lists_by_suffix) != all_suffixes:
            continue
        # We're using itertools.product to go from this:
        # areq_lists_by_suffix = {
        #     '':   [areq__A,   areq__B,   ...],
        #     '1':  [areq_1_A,  areq_1_B,  ...],
        #     ...
        #     '42': [areq_42_A, areq_42_B, ...],
        # }
        # to this:
        # [ [areq__A, areq_1_A, ..., areq_42_A],  Each of these lists is one
        #   [areq__A, areq_1_A, ..., areq_42_B],  areq_list in the loop below.
        #   [areq__A, areq_1_B, ..., areq_42_A],  each areq_list contains one
        #   [areq__A, areq_1_B, ..., areq_42_B],  AllocationRequest from each
        #   [areq__B, areq_1_A, ..., areq_42_A],  RequestGroup. So taken as a
        #   [areq__B, areq_1_A, ..., areq_42_B],  whole, each list is a viable
        #   [areq__B, areq_1_B, ..., areq_42_A],  (preliminary) candidate to
        #   [areq__B, areq_1_B, ..., areq_42_B],  return.
        #   ...,
        # ]
        for areq_list in itertools.product(
                *list(areq_lists_by_suffix.values())):
            # At this point, each AllocationRequest in areq_list is still
            # marked as use_same_provider. This is necessary to filter by group
            # policy, which enforces how these interact with each other.
            # TODO(efried): Move _satisfies_group_policy to rw_ctx?
            if not _satisfies_group_policy(
                    areq_list, rw_ctx.group_policy, num_granular_groups):
                continue
            if not _satisfies_same_subtree(
                    areq_list, rw_ctx.same_subtrees, parent_uuid_by_rp_uuid):
                continue
            # Now we go from this (where 'arr' is AllocationRequestResource):
            # [ areq__B(arrX, arrY, arrZ),
            #   areq_1_A(arrM, arrN),
            #   ...,
            #   areq_42_B(arrQ)
            # ]
            # to this:
            # areq_combined(arrX, arrY, arrZ, arrM, arrN, arrQ)
            # Note that the information telling us which RequestGroup led to
            # which piece of the AllocationRequest has been lost from the outer
            # layer of the data structure (the key of areq_lists_by_suffix).
            # => We needed that to be present for the previous filter; we need
            # it to be *absent* for the next one.
            # => However, it still exists embedded in each
            # AllocationRequestResource. That's needed to construct the
            # mappings for the output.
            areq = _consolidate_allocation_requests(areq_list)
            # Since we sourced this AllocationRequest from multiple
            # *independent* queries, it's possible that the combined result
            # now exceeds capacity where amounts of the same RP+RC were
            # folded together.  So do a final capacity check/filter.
            # TODO(efried): Move _exceeds_capacity to rw_ctx?
            if _exceeds_capacity(areq, psum_res_by_rp_rc):
                continue
            areqs.add(areq)

    # It's possible we've filtered out everything.  If so, short out.
    if not areqs:
        return [], []

    # Now we have to produce provider summaries.  The provider summaries in
    # the candidates input contain all the information; we just need to
    # filter it down to only the providers in trees represented by our merged
    # list of allocation requests.
    tree_uuids = set()
    for areq in areqs:
        for arr in areq.resource_requests:
            tree_uuids.add(arr.resource_provider.root_provider_uuid)
    psums = [psum for psum in all_psums if
             psum.resource_provider.root_provider_uuid in tree_uuids]

    LOG.debug('Merging candidates yields %d allocation requests and %d '
              'provider summaries', len(areqs), len(psums))
    return list(areqs), psums


def _rp_rc_key(rp, rc):
    """Creates hashable key unique to a provider + resource class."""
    return rp.uuid, rc


def _satisfies_group_policy(areqs, group_policy, num_granular_groups):
    """Applies group_policy to a list of AllocationRequest.

    Returns True or False, indicating whether this list of
    AllocationRequest satisfies group_policy, as follows:

    * "isolate": Each AllocationRequest with use_same_provider=True
                 is satisfied by a single resource provider.  If the "isolate"
                 policy is in effect, each such AllocationRequest must be
                 satisfied by a *unique* resource provider.
    * "none" or None: Always returns True.

    :param areqs: A list containing one AllocationRequest for each input
            RequestGroup.
    :param group_policy: String indicating how RequestGroups should interact
            with each other.  If the value is "isolate", we will return False
            if AllocationRequests that came from RequestGroups keyed by
            nonempty suffixes are satisfied by the same provider.
    :param num_granular_groups: The number of granular (use_same_provider=True)
            RequestGroups in the request.
    :return: True if areqs satisfies group_policy; False otherwise.
    """
    if group_policy != 'isolate':
        # group_policy="none" means no filtering
        return True

    # The number of unique resource providers referenced in the request groups
    # having use_same_provider=True must be equal to the number of granular
    # groups.
    num_granular_groups_in_areqs = len(set().union(*(
        # We can reliably use the first value of provider uuids in mappings:
        # all the resource_requests are satisfied by the same provider
        # by definition because use_same_provider is True.
        list(areq.mappings.values())[0] for areq in areqs
        if areq.use_same_provider)))
    if num_granular_groups == num_granular_groups_in_areqs:
        return True
    LOG.debug('Excluding the following set of AllocationRequest because '
              'group_policy=isolate and the number of granular groups in the '
              'set (%d) does not match the number of granular groups in the '
              'request (%d): %s',
              num_granular_groups_in_areqs, num_granular_groups, str(areqs))
    return False


def _satisfies_same_subtree(
        areqs, same_subtrees, parent_uuid_by_rp_uuid):
    """Applies same_subtree policy to a list of AllocationRequest.

    :param areqs: A list containing one AllocationRequest for each input
            RequestGroup.
    :param same_subtrees: A list of sets of request group suffixes strings.
            If provided, all of the resource providers satisfying the specified
            request groups must be rooted at one of the resource providers
            satisfying the request groups.
    :param parent_uuid_by_rp_uuid: A dict of parent uuids keyed by rp uuids.
    :return: True if areqs satisfies same_subtree policy; False otherwise.
    """
    for same_subtree in same_subtrees:
        # Collect RP uuids that must satisfy a single same_subtree constraint.
        rp_uuids = set().union(*(areq.mappings.get(suffix) for areq in areqs
                               for suffix in same_subtree
                               if areq.mappings.get(suffix)))
        if not _check_same_subtree(rp_uuids, parent_uuid_by_rp_uuid):
            return False
    return True


def _check_same_subtree(rp_uuids, parent_uuid_by_rp_uuid):
    """Returns True if given rp uuids are all in the same subtree.

    Note: The rps are in the same subtree means all the providers are
          rooted at one of the providers
    """
    if len(rp_uuids) == 1:
        return True
    # A set of uuids of common ancestors of each rp in question
    common_ancestors = set.intersection(*(
        _get_ancestors_by_one_uuid(rp_uuid, parent_uuid_by_rp_uuid)
        for rp_uuid in rp_uuids))
    # if any of the rp_uuid is in the common_ancestors set, then
    # we know that, that rp_uuid is the root of the other rp_uuids
    # in this same_subtree constraint.
    return len(common_ancestors.intersection(rp_uuids)) != 0


def _get_ancestors_by_one_uuid(
        rp_uuid, parent_uuid_by_rp_uuid, ancestors=None):
    """Returns a set of uuids of ancestors for a given rp uuid"""
    if ancestors is None:
        ancestors = set([rp_uuid])
    parent_uuid = parent_uuid_by_rp_uuid[rp_uuid]
    if parent_uuid is None:
        return ancestors
    ancestors.add(parent_uuid)
    return _get_ancestors_by_one_uuid(
        parent_uuid, parent_uuid_by_rp_uuid, ancestors=ancestors)
