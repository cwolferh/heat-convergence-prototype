import logging

from .framework import datastore

from . import dependencies
from . import resource
from . import template


logger = logging.getLogger('stack')

stacks = datastore.Datastore('Stack',
                             'key', 'name', 'tmpl_key', 'prev_tmpl_key')


class Stack(object):
    def __init__(self, name, tmpl, prev_tmpl_key=None, key=None):
        self.key = key
        self.tmpl = tmpl
        self.data = {
            'name': name,
            'tmpl_key': tmpl.key,
            'prev_tmpl_key': prev_tmpl_key,
        }

    def __str__(self):
        return '<Stack %r>' % self.key

    @classmethod
    def load(cls, key):
        s = stacks.read(key)
        return cls(s.name, template.Template.load(s.tmpl_key), s.prev_tmpl_key,
                   key=s.key)

    @classmethod
    def load_by_name(cls, stack_name):
        candidates = list(stacks.find(name=stack_name))
        if not candidates:
            raise stacks.NotFound('Stack "%s" not found' % stack_name)
        assert len(candidates) == 1, 'Multiple stacks "%s" found' % stack_name
        return cls.load(candidates[0])

    def store(self):
        if self.key is None:
            self.key = stacks.create(**self.data)
        else:
            stacks.update(self.key, **self.data)

    def create(self):
        self.store()
        self._create_or_update()

    def update(self, tmpl):
        old_tmpl, self.tmpl = self.tmpl, tmpl
        self.data['tmpl_key'] = tmpl.key

        logger.info('[%s(%d)] Updating...' % (self.data['name'], self.key))
        self._create_or_update(old_tmpl.key)

    @staticmethod
    def _dependencies(existing_resources,
                      current_template_deps, current_resources):
        def make_graph_key(res_name):
            return (resource.GraphKey(res_name,
                                      current_resources[res_name].key),
                    True)

        deps = current_template_deps.translate(make_graph_key)

        for key, rsrc in existing_resources.items():
            deps += (key, False), None

            # Note: reversed edges as this is the cleanup part of the graph
            for requirement in rsrc.requirements:
                if requirement in existing_resources:
                    deps += (requirement, False), (key, False)
            if rsrc.replaces in existing_resources:
                deps += ((resource.GraphKey(rsrc.name,
                                            rsrc.replaces), False),
                         (key, False))

            if rsrc.name in current_template_deps:
                deps += (key, False), make_graph_key(rsrc.name)

        return deps

    def delete(self):
        old_tmpl, self.tmpl = self.tmpl, template.Template()
        self.data['tmpl_key'] = None

        logger.info('[%s(%d)] Deleting...' % (self.data['name'], self.key))
        self._create_or_update(old_tmpl.key)

    def rollback(self):
        old_tmpl_key = self.data['prev_tmpl_key']
        if old_tmpl_key == self.tmpl.key:
            # Nothing to roll back
            return

        if old_tmpl_key is None:
            self.tmpl = template.Template()
        else:
            self.tmpl = template.Template.load(old_tmpl_key)

        current_tmpl_key = self.data['tmpl_key']
        self.data['tmpl_key'] = old_tmpl_key

        logger.info('[%s(%d)] Rolling back to template %s',
                    self.data['name'], self.key, old_tmpl_key)

        self._create_or_update(current_tmpl_key)

    def _create_or_update(self, current_tmpl_key=None):
        self.store()

        definitions = self.tmpl.resources
        tmpl_deps = self.tmpl.dependencies()
        logger.debug('[%s(%d)] Dependencies: %s' % (self.data['name'],
                                                    self.key,
                                                    tmpl_deps.graph()))

        ext_rsrcs = set(resource.Resource.load_all_from_stack(self))

        def key(r):
            return resource.GraphKey(r, rsrcs[r].key)

        def best_existing_resource(rsrc_name):
            candidate = None

            for rsrc in ext_rsrcs:
                if rsrc.name != rsrc_name:
                    continue

                if rsrc.template_key == self.tmpl.key:
                    return rsrc
                elif rsrc.template_key == current_tmpl_key:
                    candidate = rsrc

            return candidate

        def get_resource(rsrc_name):
            rsrc = best_existing_resource(rsrc_name)
            if rsrc is None:
                rsrc = resource.Resource(rsrc_name, self,
                                         definitions[rsrc_name], self.tmpl.key)

            rqrs = set(key(r) for r in tmpl_deps.required_by(rsrc_name))
            rsrc.requirers = rsrc.requirers | rqrs

            return rsrc

        rsrcs = {}
        for rsrc_name in reversed(tmpl_deps):
            rsrc = get_resource(rsrc_name)
            rsrc.store()
            rsrcs[rsrc_name] = rsrc

        dependencies = self._dependencies({resource.GraphKey(r.name, r.key): r
                                               for r in ext_rsrcs},
                                          tmpl_deps, rsrcs)

        list(dependencies)  # Check for circular deps

        from . import processes
        for graph_key, forward in dependencies.leaves():
            processes.converger.check_resource(graph_key, self.tmpl.key,
                                               {}, dependencies, forward)

    def mark_complete(self, template_key):
        if template_key != self.tmpl.key:
            return

        logger.info('[%s(%d)] update to template %d complete',
                    self.data['name'], self.key, template_key)

        prev_prev_key = self.data['prev_tmpl_key']
        self.data['prev_tmpl_key'] = template_key
        self.store()

        if prev_prev_key is not None and prev_prev_key != template_key:
            template.templates.delete(prev_prev_key)
