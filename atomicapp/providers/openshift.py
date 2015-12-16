"""
 Copyright 2015 Red Hat, Inc.

 This file is part of Atomic App.

 Atomic App is free software: you can redistribute it and/or modify
 it under the terms of the GNU Lesser General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.

 Atomic App is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU Lesser General Public License for more details.

 You should have received a copy of the GNU Lesser General Public License
 along with Atomic App. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import anymarkup
import requests
from urlparse import urljoin
from atomicapp.plugin import Provider, ProviderFailedException
from atomicapp.constants import (ACCESS_TOKEN_KEY,
                                 ANSWERS_FILE,
                                 DEFAULT_NAMESPACE,
                                 NAMESPACE_KEY,
                                 PROVIDER_API_KEY)

import logging
logger = logging.getLogger(__name__)


class OpenShiftProvider(Provider):
    key = "openshift"
    cli_str = "oc"
    cli = None
    config_file = None
    template_data = None
    providerapi = "https://127.0.0.1:8443"
    openshift_api = None
    kubernetes_api = None
    access_token = None
    namespace = DEFAULT_NAMESPACE
    ssl_verify = False

    # Parsed artifacts. Key is kind of artifacts. Value is list of artifacts.
    openshift_artifacts = {}

    def init(self):
        self.openshift_artifacts = {}

        (self.providerapi, self.access_token, self.namespace) = \
            self._get_config_values()

        # construct full urls for api endpoints
        self.kubernetes_api = urljoin(self.providerapi, "api/v1/")
        self.openshift_api = urljoin(self.providerapi, "oapi/v1/")

        logger.debug("kubernetes_api = %s", self.kubernetes_api)
        logger.debug("openshift_api = %s", self.openshift_api)

        self.oapi_resources = self._get_oapi_resources(self.providerapi)
        self.kapi_resources = self._get_kapi_resources(self.providerapi)

        self._process_artifacts()

    def _get_kapi_resources(self):
        """
        Get kubernetes API resources
        """
        # get list of supported resources for each api
        (status_code, return_data) = self._make_request("get",
                                                        self.kubernetes_api)
        if status_code == 200:
            kapi_resources = return_data["resources"]
        else:
            raise ProviderFailedException("Cannot get Kubernetes resource list")

        # convert resources list of dicts to list of names
        kapi_resources = [res['name'] for res in kapi_resources]

        logger.debug("Kubernetes resources %s", kapi_resources)

        return kapi_resources

    def _get_oapi_resources(self):
        """
        Get Openshift API resources
        """
        # get list of supported resources for each api
        (status_code, return_data) = self._make_request("get",
                                                        self.openshift_api)
        if status_code == 200:
            oapi_resources = return_data["resources"]
        else:
            raise ProviderFailedException("Cannot get OpenShift resource list")

        # convert resources list of dicts to list of names
        oapi_resources = [res['name'] for res in oapi_resources]

        logger.debug("Openshift resources %s", oapi_resources)

        return oapi_resources

    def _get_namespace(self, artifact):
        """
        Return namespace for artifact. If namespace is specified inside
        artifact use that, if not return default namespace (as specfied in
        answers.conf)

        Args:
            artifact (dict): OpenShift/Kubernetes object

        Returns:
            namespace (str)
        """
        if "metadata" in artifact and "namespace" in artifact["metadata"]:
            return artifact["metadata"]["namespace"]
        return self.namespace

    def deploy(self):
        logger.debug("Deploying to OpenShift")
        # TODO: remove running components if one component fails issue:#428
        for kind, objects in self.openshift_artifacts.iteritems():
            for artifact in objects:
                namespace = self._get_namespace(artifact)
                url = self._get_url(namespace, kind)

                if self.dryrun:
                    logger.info("DRY-RUN: %s", url)
                    continue
                (status_code, return_data) = \
                    self._make_request("post", url, data=artifact)
                if status_code == 201:
                    logger.info("Object %s sucessfully deployed.",
                                artifact['metadata']['name'])
                else:
                    msg = "%s %s" % (status_code, return_data)
                    logger.error(msg)
                    # TODO: remove running components (issue: #428)
                    raise ProviderFailedException(msg)

    def _process_artifacts(self):
        """
        Parse OpenShift manifests files and checks if manifest under
        process is valid. Reads self.artifacts and saves parsed artifacts
        to self.openshift_artifacts
        """
        for artifact in self.artifacts:
            logger.debug("Processing artifact: %s", artifact)
            data = None
            with open(os.path.join(self.path, artifact), "r") as fp:
                data = anymarkup.parse(fp, force_types=None)
            # kind has to be specified in artifact
            if "kind" not in data.keys():
                raise ProviderFailedException(
                    "Error processing %s artifact. There is no kind" % artifact)

            kind = data["kind"].lower()
            resource = self._kind_to_resource(kind)

            # check if resource is supported by apis
            if resource not in self.oapi_resources \
                    and resource not in self.kapi_resources:
                raise ProviderFailedException(
                    "Unsupported kind %s in artifact %s" % (kind, artifact))

            # process templates
            if kind == "template":
                processed_objects = self._process_template(data)
                # add all processed object to artifacts dict
                for obj in processed_objects:
                    obj_kind = obj["kind"].lower()
                    if obj_kind not in self.openshift_artifacts.keys():
                        self.openshift_artifacts[obj_kind] = []
                    self.openshift_artifacts[obj_kind].append(obj)
                continue

            # add parsed artifact to dict
            if kind not in self.openshift_artifacts.keys():
                self.openshift_artifacts[kind] = []
            self.openshift_artifacts[kind].append(data)

    def _process_template(self, template):
        """
        Call OpenShift api and process template.
        Templates allow parameterization of resources prior to being sent to
        the server for creation or update. Templates have "parameters",
        which may either be generated on creation or set by the user.

        Args:
            template (dict): template to process

        Returns:
            List of objects from processed template.
        """
        logger.debug("processing template: %s", template)
        url = self._get_url(self._get_namespace(template), "processedtemplates")
        (status_code, return_data) = self._make_request("post", url, data=template)
        if status_code == 201:
            logger.info("template proccessed %s", template['metadata']['name'])
            logger.debug("processed template %s", return_data)
            return return_data['objects']
        else:
            msg = "%s %s" % (status_code, return_data)
            logger.error(msg)
            raise ProviderFailedException(msg)

    def _kind_to_resource(self, kind):
        """
        Converts kind to resource name. It is same logics
        as in k8s.io/kubernetes/pkg/api/meta/restmapper.go (func KindToResource)
        Example:
            Pod -> pods
            Policy - > policies
            BuildConfig - > buildconfigs

        Args:
            kind (str): Kind of the object

        Returns:
            Resource name (str) (kind in plural form)
        """
        singular = kind.lower()
        if singular.endswith("status"):
            plural = singular + "es"
        else:
            if singular[-1] == "s":
                plural = singular
            elif singular[-1] == "y":
                plural = singular.rstrip("y") + "ies"
            else:
                plural = singular + "s"
        return plural

    def _get_url(self, namespace, kind, name=None):
        """
        Some kinds/resources are managed by OpensShift and some by Kubernetes.
        Here we compose right url (Kubernets or OpenShift) for given kind.
        If resource is managed by Kubernetes or OpenShift is determined by
        self.kapi_resources/self.oapi_resources lists
        Example:
            For namespace=project1, kind=DeploymentConfig, name=dc1 result
            would be http://example.com:8443/oapi/v1/namespaces/project1/deploymentconfigs/dc1

        Args:
            namespace (str): Kubernetes namespace or Openshift project name
            kind (str): kind of the object
            name (str): object name if modifying or deleting specific object (optional)

        Returns:
            Full url (str) for given kind, namespace and name
        """
        url = None

        resource = self._kind_to_resource(kind)

        if resource in self.oapi_resources:
            url = self.openshift_api
        elif resource in self.kapi_resources:
            url = self.kubernetes_api

        url = urljoin(url, "namespaces/%s/%s/" % (namespace, resource))

        if name:
            url = urljoin(url, name)

        url = urljoin(url, "?access_token={}".format(self.access_token))
        logger.debug("url: %s", url)
        return url

    def _parse_kubeconf(self, filename):
        """"
        Parse kubectl config file

        Args:
            filename (string): path to configuration file (e.g. ./kube/config)

        Returns:
            tuple with server url, oauth token and namespace
            (url, token, namespace)

        Example of expected file format:
            apiVersion: v1
            clusters:
            - cluster:
                server: https://10.1.2.2:8443
              name: 10-1-2-2:8443
            contexts:
            - context:
                cluster: 10-1-2-2:8443
                namespace: test
                user: test-admin/10-1-2-2:8443
              name: test/10-1-2-2:8443/test-admin
            current-context: test/10-1-2-2:8443/test-admin
            kind: Config
            preferences: {}
            users:
            - name: test-admin/10-1-2-2:8443
            user:
                token: abcdefghijklmnopqrstuvwxyz0123456789ABCDEF
        """

        url = None
        token = None
        namespace = None

        logger.debug("Parsing %s", filename)

        with open(filename, 'r') as fp:
            kubecfg = anymarkup.parse(fp.read())

        current_context = kubecfg["current-context"]

        logger.debug("current context: %s", current_context)

        context = None
        for co in kubecfg["contexts"]:
            if co["name"] == current_context:
                context = co

        cluster = None
        for cl in kubecfg["clusters"]:
            if cl["name"] == context["context"]["cluster"]:
                cluster = cl

        user = None
        for usr in kubecfg["users"]:
            if usr["name"] == context["context"]["user"]:
                user = usr

        if not context or not cluster or not user:
            raise ProviderFailedException("Invalid %s", filename)

        logger.debug("context: %s", context)
        logger.debug("cluster: %s", cluster)
        logger.debug("user: %s", user)

        url = cluster["cluster"]["server"]
        token = user["user"]["token"]
        if "namespace" in context["context"]:
            namespace = context["context"]["namespace"]

        return (url, token, namespace)

    def _get_config_values(self):
        """
        Reads providerapi, namespace and accesstoken from answers.conf and
        corresponding values from providerconfig (if set).
        Use one that is set, if both are set and have conflicting values raise
        exception.

        Returns:
            tuple (providerapi, accesstoken, providerapi)

        Raises:
            ProviderFailedException: values in providerconfig and answers.conf
                are in conflict

        """
        result = {"namespace": self.namespace,
                  "access_token": self.access_token,
                  "providerapi": self.providerapi}

        answers = {"namespace": None,
                   "access_token": None,
                   "providerapi": None}
        providerconfig = {"namespace": None,
                          "access_token": None,
                          "providerapi": None}

        answers["namespace"] = self.config.get(NAMESPACE_KEY)
        answers["access_token"] = self.config.get(ACCESS_TOKEN_KEY)
        answers["providerapi"] = self.config.get(PROVIDER_API_KEY)

        if self.config_file:
            (providerconfig["providerapi"], providerconfig["access_token"],
             providerconfig["namespace"]) = self._parse_kubeconf(self.config_file)

        # decide between values from answers.conf and providerconfig
        # if only one is set use that, report if they are in conflict
        for k in ["namespace", "access_token", "providerapi"]:
            if answers[k] and not providerconfig[k]:
                result[k] = answers[k]
            if not answers[k] and providerconfig[k]:
                result[k] = providerconfig[k]
            if answers[k] and providerconfig[k]:
                if answers[k] == providerconfig[k]:
                    result[k] = answers[k]
                else:
                    msg = "There are conflicting values in %s (%s) and %s (%s)"\
                        % (self.config_file, providerconfig[k], ANSWERS_FILE,
                           answers[k])
                    logger.error(msg)
                    raise ProviderFailedException(msg)
        return (result["providerapi"],
                result["access_token"],
                result["namespace"])

    def _make_request(self, method, url, data=None):
        """
        Make HTTP request to url

        Args:
            method (str): http method (post/get/delete)
            url (str): url
            data (dict/list): object to be serialised to json and send as http data
                              (when method=post)

        Returns:
            tuple (status_code, return_data): status_code - http status code
                                              return_data - deserialised json object

        Raises:
            ProviderFailedException: connect or read timeout when communicating
                                     with api
        """

        status_code = None
        return_data = None

        try:
            if method.lower() == "get":
                res = requests.get(url, verify=self.ssl_verify)
            elif method.lower() == "post":
                res = requests.post(url, json=data, verify=self.ssl_verify)
            elif method.lower() == "delete":
                res = requests.delete(url, json=data, verify=self.ssl_verify)

            status_code = res.status_code
            return_data = res.json()
        except requests.exceptions.ConnectTimeout:
            msg = "Timeout when connecting to  %s" % url
            logger.error(msg)
            raise ProviderFailedException(msg)
        except requests.exceptions.ReadTimeout:
            msg = "Timeout when reading from %s" % url
            logger.error(msg)
            raise ProviderFailedException(msg)
        except ValueError:
            # invalid json
            return_data = None

        return (status_code, return_data)
