#
#  Copyright 2018 Red Hat | Ansible
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import, division, print_function
__metaclass__ = type

import copy

from ansible_collections.community.kubernetes.plugins.module_utils.common import AUTH_ARG_SPEC, RESOURCE_ARG_SPEC, NAME_ARG_SPEC
from ansible_collections.community.kubernetes.plugins.module_utils.common import KubernetesAnsibleModule
from ansible.module_utils.six import string_types

try:
    import yaml
    from openshift.dynamic.exceptions import NotFoundError
except ImportError:
    pass


SCALE_ARG_SPEC = {
    'replicas': {'type': 'int', 'required': True},
    'current_replicas': {'type': 'int'},
    'resource_version': {},
    'wait': {'type': 'bool', 'default': True},
    'wait_timeout': {'type': 'int', 'default': 20},
}


class KubernetesAnsibleScaleModule(KubernetesAnsibleModule):

    def __init__(self, k8s_kind=None, *args, **kwargs):
        self.client = None
        self.warnings = []

        mutually_exclusive = [
            ('resource_definition', 'src'),
        ]

        KubernetesAnsibleModule.__init__(self, *args,
                                         mutually_exclusive=mutually_exclusive,
                                         supports_check_mode=True,
                                         **kwargs)
        self.kind = k8s_kind or self.params.get('kind')
        self.api_version = self.params.get('api_version')
        self.name = self.params.get('name')
        self.namespace = self.params.get('namespace')
        resource_definition = self.params.get('resource_definition')

        if resource_definition:
            if isinstance(resource_definition, string_types):
                try:
                    self.resource_definitions = yaml.safe_load_all(resource_definition)
                except (IOError, yaml.YAMLError) as exc:
                    self.fail(msg="Error loading resource_definition: {0}".format(exc))
            elif isinstance(resource_definition, list):
                self.resource_definitions = resource_definition
            else:
                self.resource_definitions = [resource_definition]
        src = self.params.get('src')
        if src:
            self.resource_definitions = self.load_resource_definitions(src)

        if not resource_definition and not src:
            implicit_definition = dict(
                kind=self.kind,
                apiVersion=self.api_version,
                metadata=dict(name=self.name)
            )
            if self.namespace:
                implicit_definition['metadata']['namespace'] = self.namespace
            self.resource_definitions = [implicit_definition]

    def execute_module(self):
        definition = self.resource_definitions[0]

        self.client = self.get_api_client()

        name = definition['metadata']['name']
        namespace = definition['metadata'].get('namespace')
        api_version = definition['apiVersion']
        kind = definition['kind']
        current_replicas = self.params.get('current_replicas')
        replicas = self.params.get('replicas')
        resource_version = self.params.get('resource_version')

        wait = self.params.get('wait')
        wait_time = self.params.get('wait_timeout')
        existing = None
        existing_count = None
        return_attributes = dict(changed=False, result=dict(), diff=dict())
        if wait:
            return_attributes['duration'] = 0

        resource = self.find_resource(kind, api_version, fail=True)

        try:
            existing = resource.get(name=name, namespace=namespace)
            return_attributes['result'] = existing.to_dict()
        except NotFoundError as exc:
            self.fail_json(msg='Failed to retrieve requested object: {0}'.format(exc),
                           error=exc.value.get('status'))

        if self.kind == 'job':
            existing_count = existing.spec.parallelism
        elif hasattr(existing.spec, 'replicas'):
            existing_count = existing.spec.replicas

        if existing_count is None:
            self.fail_json(msg='Failed to retrieve the available count for the requested object.')

        if resource_version and resource_version != existing.metadata.resourceVersion:
            self.exit_json(**return_attributes)

        if current_replicas is not None and existing_count != current_replicas:
            self.exit_json(**return_attributes)

        if existing_count != replicas:
            return_attributes['changed'] = True
            if not self.check_mode:
                if self.kind == 'job':
                    existing.spec.parallelism = replicas
                    return_attributes['result'] = resource.patch(existing.to_dict()).to_dict()
                else:
                    return_attributes = self.scale(resource, existing, replicas, wait, wait_time)

        self.exit_json(**return_attributes)

    @property
    def argspec(self):
        args = copy.deepcopy(SCALE_ARG_SPEC)
        args.update(RESOURCE_ARG_SPEC)
        args.update(NAME_ARG_SPEC)
        args.update(AUTH_ARG_SPEC)
        return args

    def scale(self, resource, existing_object, replicas, wait, wait_time):
        name = existing_object.metadata.name
        namespace = existing_object.metadata.namespace
        kind = existing_object.kind

        if not hasattr(resource, 'scale'):
            self.fail_json(
                msg="Cannot perform scale on resource of kind {0}".format(resource.kind)
            )

        scale_obj = {'kind': kind, 'metadata': {'name': name, 'namespace': namespace}, 'spec': {'replicas': replicas}}

        existing = resource.get(name=name, namespace=namespace)

        try:
            resource.scale.patch(body=scale_obj)
        except Exception as exc:
            self.fail_json(msg="Scale request failed: {0}".format(exc))

        k8s_obj = resource.get(name=name, namespace=namespace).to_dict()
        match, diffs = self.diff_objects(existing.to_dict(), k8s_obj)
        result = dict()
        result['result'] = k8s_obj
        result['changed'] = not match
        result['diff'] = diffs

        if wait:
            success, result['result'], result['duration'] = self.wait(resource, scale_obj, 5, wait_time)
            if not success:
                self.fail_json(msg="Resource scaling timed out", **result)
        return result
