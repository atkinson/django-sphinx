#coding: utf-8

__author__ = 'ego'

import re
import time
import warnings
from collections import OrderedDict
from datetime import datetime, date

try:
    import decimal
except ImportError:
    from django.utils import _decimal as decimal  # for Python 2.3

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.query import QuerySet

from django.utils.encoding import force_unicode

from djangosphinx.conf import *
from djangosphinx.constants import PASSAGES_OPTIONS, EMPTY_RESULT_SET, QUERY_OPTIONS, QUERY_RANKERS,\
    FILTER_CMP_OPERATIONS, FILTER_CMP_INVERSE

from djangosphinx.query.proxy import SphinxProxy
from djangosphinx.query.query import SphinxQuery, conn_handler
from djangosphinx.utils.config import get_sphinx_attr_type_for_field


__all__ = ['SearchError', 'ConnectionError', 'BaseSphinxQuerySet', 'SphinxProxy',
           'to_sphinx']

def to_sphinx(value):
   "Convert a value into a sphinx query value"
   if isinstance(value, date) or isinstance(value, datetime):
       return int(time.mktime(value.timetuple()))
   elif isinstance(value, decimal.Decimal) or isinstance(value, float):
       return float(value)
   return int(value)



class SearchError(Exception):
    pass

class SphinxQuerySet(object):

    __index_match = re.compile(r'[^a-z0-9_-]*', re.I)

    def __init__(self, model=None, using=None, **kwargs):
        self.model = model
        self.using = using
        self._db = conn_handler.connection

        self._iter = None

        self._query = None
        self._query_args = None

        self._field_names = {}
        self._fields = '*'
        self._aliases = {}
        self._group_by = ''
        self._order_by = ''
        self._group_order_by = ''

        self._filters = {}
        self._excludes = {}

        self._query_opts = None

        self._limit = 20
        self._offset = 0

        self._query_string = ''
        self._result_cache = None
        self._fields_cache = {}
        self._metadata = None

        self._maxmatches = SPHINX_MAX_MATCHES

        self._passages = SPHINX_PASSAGES
        self._passages_opts = {}
        self._passages_string = {}

        if model:
            self._indexes = self._parse_indexes(kwargs.pop('index', model._meta.db_table))
        else:
            self._indexes = self._parse_indexes(kwargs.pop('index', None))

        self._index = self.index

    def __len__(self):
        return self.count()

    def __iter__(self):
        if self._iter is None:
            self._get_data()

        return iter(self._result_cache)

    def __repr__(self):
        return repr(self.__iter__())

    def __getitem__(self, k):
        """
        Retrieves an item or slice from the set of results.
        """
        if not isinstance(k, (slice, int, long)):
            raise TypeError
        assert ((not isinstance(k, slice) and (k >= 0))
                or (isinstance(k, slice) and (k.start is None or k.start >= 0)
                    and (k.stop is None or k.stop >= 0))),\
        "Negative indexing is not supported."

        # no cache now
        if self._result_cache is not None:
            if self._iter is not None:
                # The result cache has only been partially populated, so we may
                # need to fill it out a bit more.
                if isinstance(k, slice):
                    if k.stop is not None:
                        # Some people insist on passing in strings here.
                        bound = int(k.stop)
                    else:
                        bound = None
                else:
                    bound = k + 1
                if len(self._result_cache) < bound:
                    self._fill_cache(bound - len(self._result_cache))
            return self._result_cache[k]

        if isinstance(k, slice):
            qs = self._clone()
            if k.start is not None:
                start = int(k.start)
            else:
                start = None
            if k.stop is not None:
                stop = int(k.stop)
            else:
                stop = None
            qs.set_limits(start, stop)
            qs._fill_cache()
            return k.step and list(qs)[::k.step] or qs

        try:
            qs = self._clone()
            qs.set_limits(k, k + 1)
            qs._fill_cache()
            return list(qs)[0]
        except Exception, e:
            raise IndexError(e.args)

    def next(self):
        raise NotImplementedError

    # indexes

    def add_index(self, index):
        _indexes = self._indexes[:]

        for x in self._parse_indexes(index):
            if x not in _indexes:
                _indexes.append(x)

        return self._clone(_indexes=_indexes)

    def remove_index(self, index):
        _indexes = self._indexes[:]

        for x in self._parse_indexes(index):
            if x in _indexes:
                _indexes.pop(_indexes.index(x))

        return self._clone(_indexes=_indexes)



    # querying

    def query(self, query):
        return self._clone(_query=force_unicode(query))

    def filter(self, **kwargs):
        filters = self._filters.copy()
        return self._clone(_filters=self._process_filters(filters, False, **kwargs))

    def exclude(self, **kwargs):
        filters = self._excludes.copy()
        return self._clone(_excludes=self._process_filters(filters, True, **kwargs))

    def fields(self, *args, **kwargs):
        fields = ''
        aliases = {}
        if args:
            fields = '`%s`' % '`, `'.join(args)
        if kwargs:
            for k, v in kwargs.iteritems():
                aliases[k] = '%s AS `%s`' % (v, k)

        if fields or aliases:
            return self._clone(_fields=fields, _aliases=aliases)
        return self

    # Currently only supports grouping by a single column.
    # The column however can be a computed expression
    def group_by(self, field):
        return self._clone(_group_by='GROUP BY `%s`' % field)

    def order_by(self, *args):
        sort_by = []
        for arg in args:
            order = 'ASC'
            if arg[0] == '-':
                order = 'DESC'
                arg = arg[1:]
            if arg == 'pk':
                arg = 'id'

            sort_by.append('`%s` %s' % (arg, order))

        if sort_by:
            return self._clone(_order_by='ORDER BY %s' % ', '.join(sort_by))
        return self

    def group_order(self, *args):
        sort_by = []
        for arg in args:
            order = 'ASC'
            if arg[0] == '-':
                order = 'DESC'
                arg = arg[1:]
            if arg == 'pk':
                arg = 'id'

            sort_by.append('`%s` %s' % (arg, order))

        if sort_by:
            return self._clone(_group_order_by='WITHIN GROUP ORDER BY %s' % ', '.join(sort_by))
        return self

    def count(self):
        return min(self.meta.get('total_found', 0), self._maxmatches)

    def all(self):
       return self

    def none(self):
       qs = EmptySphinxQuerySet()
       qs.__dict__.update(self.__dict__.copy())
       return qs


    # other
    def reset(self):
           return self.__class__(self.model, self.using, index=self.index)



    def get_query_set(self, model):
        qs = model._default_manager
        if self.using is not None:
            qs = qs.db_manager(self.using)
        return qs.all()

    def escape(self, value):
        return re.sub(r"([=\(\)|\-!@~\"&/\\\^\$\=])", r"\\\1", value)



    def set_passages(self, enable=True, **kwargs):
        self._passages = enable

        for k, v in kwargs.iteritems():
            if k in PASSAGES_OPTIONS:
                assert(isinstance(v, PASSAGES_OPTIONS[k]))

                if isinstance(v, bool):
                    v = int(v)

                self._passages_opts[k] = v

        self._passages_string = None

    def _build_passages_string(self):
        opts_list = []
        for k, v in self._passages_opts.iteritems():
            opts_list.append("'%s' AS `%s`" % (self.escape(v), k))

        if opts_list:
            self._passages_string = ', %s' % ', '.join(opts_list)




    def set_options(self, **kwargs):
        kwargs = self._format_options(**kwargs)
        if kwargs is not None:
            return self._clone(**kwargs)
        return self


    def set_limits(self, start=None, stop=None):
        if start is not None:
            self._offset = int(start)
        if stop is not None:
            self._limit = stop - start

        self._query_string = None

    # Properties

    def _get_index(self):
        return ' '.join(self._indexes)

    index = property(_get_index)

    def _meta(self):
        if self._metadata is None:
            self._get_data()

        return self._metadata

    meta = property(_meta)

    def _get_passages_string(self):
        if self._passages_string is None:
            self._build_passages_string()
        return self._passages_string

    passages = property(_get_passages_string)

    #internal

    def _get_data(self):
        assert(self._indexes)

        self._iter = SphinxQuery(self.query_string, self._query_args)
        self._result_cache = []
        self._metadata = self._iter.meta
        self._fill_cache()

    def _qstring(self):
        if not self._query_string:
            self._build_query()
        return self._query_string

    query_string = property(_qstring)

    # internal
    def _format_options(self, **kwargs):
        opts = []

        for k, v in kwargs.iteritems():
            if k in QUERY_OPTIONS:
                assert(isinstance(v, QUERY_OPTIONS[k]))

                if k == 'ranker':
                    assert(v in QUERY_RANKERS)
                elif k == 'reverse_scan':
                    v = int(v) & 0x0000000001
                elif k in ('field_weights', 'index_weights'):
                    v = '(%s)' % ', '.join(['%s=%s' % (x, v[x]) for x in v])
                    #elif k == 'comment':
                #    v = '\'%s\'' % self.escape(v)

                opts.append('%s=%s' % (k, v))

        if opts:
            return dict(_query_opts='OPTION %s' % ','.join(opts))
        return None


    def _fill_cache(self, num=None):
        fields = self.meta['fields'].copy()
        id_pos = fields.pop('id')
        ct = None
        results = {}

        docs = OrderedDict()

        if self._iter:
            try:
                while True:
                    doc = self._iter.next()
                    doc_id = doc[id_pos]

                    obj_id, ct = self._decode_document_id(int(doc_id))

                    results.setdefault(ct, {})[obj_id] = {}

                    docs.setdefault(doc_id, {})['results'] = results[ct][obj_id]
                    docs[doc_id]['data'] = {}

                    for field in fields:
                        docs[doc_id]['data'].setdefault('fields', {})[field] = doc[fields[field]]
            except StopIteration:
                self._iter = None

                if self.model is None and len(self._indexes) == 1 and ct is not None:
                    self.model = ContentType.objects.get(pk=ct).model_class()

                if self.model:
                    qs = self.get_query_set(self.model)

                    qs = qs.filter(pk__in=results[ct].keys())

                    for obj in qs:
                        results[ct][obj.pk]['obj'] = obj

                else:
                    for ct in results:
                        model_class = ContentType.objects.get(pk=ct).model_class()
                        qs = self.get_query_set(model_class).filter(pk__in=results[ct].keys())

                        for obj in qs:
                            results[ct][obj.pk]['obj'] = obj

                if self._passages:
                    for doc in docs.values():
                        doc['data']['passages'] = self._get_passages(doc['results']['obj'])
                        self._result_cache.append(SphinxProxy(doc['results']['obj'], doc['data']))
                else:
                    for doc in docs.values():
                        self._result_cache.append(SphinxProxy(doc['results']['obj'], doc['data']))


    def _get_passages(self, instance):
        fields = self._get_doc_fields(instance)

        docs = [getattr(instance, f) for f in fields]
        opts = self.passages if self.passages else ''

        doc_format = ', '.join('%s' for x in range(0, len(fields)))
        query = 'CALL SNIPPETS (({0:>s}), \'{1:>s}\', %s {2:>s})'.format(doc_format,
            instance.__sphinx_indexes__[0],
            opts)
        docs.append(self._query)

        c = self._db.cursor()
        count = c.execute(query, docs)

        passages = {}
        for field in fields:
            passages[field] = c.fetchone()[0].decode('utf-8')

        return passages

    def _parse_indexes(self, index):

        if index is None:
            return list()

        return [x.lower() for x in re.split(self.__index_match, index) if x]

    def _decode_document_id(self, doc_id):
        assert isinstance(doc_id, int)

        ct = (doc_id & 0xFF000000) >> DOCUMENT_ID_SHIFT
        return doc_id & 0x00FFFFFF, ct

    def _process_single_obj_operation(self, obj):
        if isinstance(obj, models.Model):
            if self.model is None:
                raise ValueError('For non model or multiple model indexes comparsion with objects not supported')
            value = obj.pk
        elif not isinstance(obj, (list, tuple, QuerySet)):
            value = obj
        else:
            raise TypeError('Comparison operations require a single object, not a `%s`' % type(obj))

        return to_sphinx(value)

    def _process_obj_list_operation(self, obj_list):
        if isinstance(obj_list, (models.Model, QuerySet)):
            if self.model is None:
                raise ValueError('For non model or multiple model indexes comparsion with objects not supported')

            if isinstance(obj_list, models.Model):
                values = [obj_list.pk]
            else:
                values = [obj.pk for obj in obj_list]

        elif hasattr(obj_list, '__iter__') or isinstance(obj_list, (list, tuple)):
            values = list(obj_list)
        elif isinstance(obj_list, (int, float, date, datetime)):
            values = [obj_list]
        else:
            raise ValueError('`%s` is not a list of objects and not single object' % type(obj_list))

        return map(to_sphinx, values)

    def _process_filters(self, filters, exclude=False, **kwargs):
        for k, v in kwargs.iteritems():
            parts = k.split('__')
            parts_len = len(parts)
            field = parts[0]
            lookup = parts[-1]

            if parts_len == 1:  # один
                #v = self._process_single_obj_operation(parts[0], v)
                filters[k] = '`%s` %s %s' % (field,
                                             '!=' if exclude else '=',
                                             self._process_single_obj_operation(v))
            elif parts_len == 2: # один exact или список, или сравнение
                if lookup == 'in':
                    filters[k] = '`%s` %sIN (%s)' % (field,
                                                     'NOT ' if exclude else '',
                                                     ','.join(str(x) for x in self._process_obj_list_operation(v)))
                elif lookup == 'range':
                    v = self._process_obj_list_operation(v)
                    if len(v) != 2:
                        raise ValueError('Range may consist of two values')
                    if exclude:
                        # not supported by sphinx. raises error!
                        warnings.warn('Exclude range not supported by SphinxQL now!')
                        filters[k] = 'NOT `%s` BETWEEN %i AND %i' % (field, v[0], v[1])
                    else:
                        filters[k] = '`%s` BETWEEN %i AND %i' % (field, v[0], v[1])

                elif lookup in FILTER_CMP_OPERATIONS:
                    filters[k] = '`%s` %s %s' % (field,
                                                 FILTER_CMP_INVERSE[lookup]\
                                                 if exclude\
                                                 else FILTER_CMP_OPERATIONS[lookup],
                                                 self._process_single_obj_operation(v))
                else:
                    raise NotImplementedError('Related object and/or field lookup "%s" not supported' % lookup)
            else: # related
                raise NotImplementedError('Related model fields lookup not supported')

        return filters

    def _get_doc_fields(self, instance):
        cache = self._fields_cache.get(type(instance), None)
        if cache is None:
            def _get_field(name):
                return instance._meta.get_field(name)

            opts = instance.__sphinx_options__
            included = opts.get('included_fields', [])
            excluded = opts.get('excluded_fields', [])
            stored_attrs = opts.get('stored_attributes', [])
            stored_fields = opts.get('stored_fields', [])
            if included:
                included = [f for f in included if
                            f not in excluded
                            and
                            get_sphinx_attr_type_for_field(_get_field(f)) == 'string']
                for f in stored_fields:
                    if get_sphinx_attr_type_for_field(_get_field(f)) == 'string':
                        included.append(f)
            else:
                included = [f.name for f in instance._meta.fields
                            if
                            f.name not in excluded
                            and
                            (f.name not in stored_attrs
                             or
                             f.name in stored_fields)
                            and
                            get_sphinx_attr_type_for_field(f) == 'string']

            cache = self._fields_cache[type(instance)] = included

        return cache

    def _build_query(self):
        self._query_args = []

        q = ['SELECT']
        if self._fields:
            q.append(self._fields)
            if self._aliases:
                q.append(',')

        if self._aliases:
            q.append(', '.join(self._aliases.values()))

        q.extend(['FROM', ', '.join(self._indexes)])

        if self._query or self._filters or self._excludes:
            q.append('WHERE')
        if self._query:
            q.append('MATCH(%s)')
            self._query_args.append(self._query)

            if self._filters or self._excludes:
                q.append('AND')
        if self._filters:
            q.append(' AND '.join(self._filters.values()))
            if self._excludes:
                q.append('AND')
        if self._excludes:
            q.append(' AND '.join(self._excludes.values()))

        if self._group_by:
            q.append(self._group_by)
        if self._order_by:
            q.append(self._order_by)
        if self._group_order_by:
            q.append(self._group_order_by)

        q.append('LIMIT %i, %i' % (self._offset, self._limit))

        if self._query_opts is not None:
            q.append(self._query_opts)

        self._query_string = ' '.join(q)

        return self._query_string

    def _clone(self, **kwargs):
        # Clones the queryset passing any changed args
        c = self.__class__()
        c.__dict__.update(self.__dict__.copy())

        c._result_cache = None
        c._query_string = None
        c._metadata = None
        c._iter = None

        for k, v in kwargs.iteritems():
            setattr(c, k, v)
            # почистим кеш в новом объекте, раз уж параметры запроса изменились

        return c


class EmptySphinxQuerySet(SphinxQuerySet):
    def _get_data(self):
        self._iter = iter([])
        self._result_cache = []
        self._metadata = EMPTY_RESULT_SET
