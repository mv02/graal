#
# Copyright (c) 2019, 2024, Oracle and/or its affiliates. All rights reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# The Universal Permissive License (UPL), Version 1.0
#
# Subject to the condition set forth below, permission is hereby granted to any
# person obtaining a copy of this software, associated documentation and/or
# data (collectively the "Software"), free of charge and under any and all
# copyright rights in the Software, and any and all patent rights owned or
# freely licensable by each licensor hereunder covering either (i) the
# unmodified Software as contributed to or provided by such licensor, or (ii)
# the Larger Works (as defined below), to deal in both
#
# (a) the Software, and
#
# (b) any piece of software and/or hardware listed in the lrgrwrks.txt file if
# one is included with the Software each a "Larger Work" to which the Software
# is contributed by such licensors),
#
# without restriction, including without limitation the rights to copy, create
# derivative works of, display, perform, and distribute the Software and make,
# use, sell, offer for sale, import, export, have made, and have sold the
# Software and the Larger Work(s), and to sublicense the foregoing rights on
# either these or other terms.
#
# This license is subject to the following condition:
#
# The above copyright notice and either this complete permission notice or at a
# minimum a reference to the UPL must be included in all copies or substantial
# portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

from __future__ import print_function

import os.path
import time
import signal
import threading
import json
import argparse
from typing import List

import mx
import mx_benchmark
import datetime
import re
import copy
import urllib.request

import mx_sdk_vm_impl


def parse_prefixed_args(prefix, args):
    ret = []
    for arg in args:
        if arg.startswith(prefix):
            parsed = arg.split(' ')[0].split(prefix)[1]
            if parsed not in ret:
                ret.append(parsed)
    return ret

def parse_prefixed_arg(prefix, args, errorMsg):
    ret = parse_prefixed_args(prefix, args)
    if len(ret) > 1:
        mx.abort(errorMsg)
    elif len(ret) < 1:
        return None
    else:
        return ret[0]


def convertValue(table, value, fromUnit, toUnit):
    if fromUnit not in table:
        mx.abort("Unexpected unit: " + fromUnit)
    fromFactor = float(table[fromUnit])

    if toUnit not in table:
        mx.abort("Unexpected unit: " + fromUnit)
    toFactor = float(table[toUnit])

    return float((value * fromFactor) / toFactor)


timeUnitTable = {
    'ns':   1,
    'us':   1000,
    'ms':   1000 * 1000,
    's':    1000 * 1000 * 1000,
    'min':  60 * 1000 * 1000 * 1000,
    'h':    60 * 60 * 1000 * 1000 * 1000
}


tputUnitTable = {
    'op/ns':    1.0,
    'op/us':    1.0/1000,
    'op/ms':    1.0/(1000 * 1000),
    'op/s':     1.0/(1000 * 1000 * 1000),
    'op/min':   1.0/(60 * 1000 * 1000 * 1000),
    'op/h':     1.0/(60 * 60 * 1000 * 1000 * 1000)
}


memUnitTable = {
    'B':    1,
    'kB':   1000,
    'MB':   1000 * 1000,
    'GB':   1000 * 1000 * 1000,
    'KiB':  1024,
    'MiB':  1024 * 1024,
    'GiB':  1024 * 1024 * 1024
}


def strip_args_with_number(strip_args, args):
    """Removes arguments (specified in `strip_args`) from `args`.

    The stripped arguments are expected to have a number value. For single character arguments (e.g. `-X`) the space
    before the value might be omitted (e.g. `-X8`). In this case only one element is removed from `args`. Otherwise
    (e.g. `-X 8`), two elements are removed from `args`.
    """

    if not isinstance(strip_args, list):
        strip_args = [strip_args]

    def _strip_arg_with_number_gen(_strip_arg, _args):
        skip_next = False
        for arg in _args:
            if skip_next:
                # skip value of argument
                skip_next = False
                continue
            if arg.startswith(_strip_arg):
                if arg == _strip_arg:
                    # full match - value is the next argument `-i 10`
                    skip_next = True
                    continue
                # partial match at begin - either a different option or value without space separator `-i10`
                if len(_strip_arg) == 2 and _strip_arg.startswith('-'):
                    # only look at single character options
                    remainder_arg = arg[len(_strip_arg):]
                    try:
                        int(remainder_arg)
                        # remainder is a number - skip the current arg
                        continue
                    except ValueError:
                        # not a number - probably a different option
                        pass
            # add arg to result
            yield arg

    result = args
    for strip_arg in strip_args:
        result = _strip_arg_with_number_gen(strip_arg, result)
    return list(result)


class NativeImageBenchmarkMixin(object):

    def __init__(self):
        self.benchmark_name = None

    def benchmarkName(self):
        if not self.benchmark_name:
            raise NotImplementedError()
        return self.benchmark_name

    def run_stage(self, vm, stage, command, out, err, cwd, nonZeroIsFatal):
        final_command = command
        if stage == 'run':
            final_command = self.apply_command_mapper_hooks(command, vm)

        return mx.run(final_command, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)

    def apply_command_mapper_hooks(self, cmd, vm):
        return mx.apply_command_mapper_hooks(cmd, vm.command_mapper_hooks)

    def extra_image_build_argument(self, _, args):
        return parse_prefixed_args('-Dnative-image.benchmark.extra-image-build-argument=', args)

    def extraVmArgs(self):
        assert self.dist
        distribution = mx.distribution(self.dist)
        assert distribution.isJARDistribution()
        jdk = mx.get_jdk(distribution.javaCompliance)
        add_opens_add_extracts = []
        if mx_benchmark.mx_benchmark_compatibility().jmh_dist_benchmark_extracts_add_opens_from_manifest():
            add_opens_add_extracts = mx_benchmark._add_opens_and_exports_from_manifest(distribution.path)
        return mx.get_runtime_jvm_args([self.dist], jdk=jdk, exclude_names=mx_sdk_vm_impl.NativePropertiesBuildTask.implicit_excludes) + add_opens_add_extracts

    def extra_jvm_arg(self, benchmark, args):
        return parse_prefixed_args('-Dnative-image.benchmark.extra-jvm-arg=', args)

    def extra_run_arg(self, benchmark, args, image_run_args):
        """Returns all arguments passed to the final image.

        This includes those passed globally on the `mx benchmark` command line after the last `--`.
        These arguments are passed via the `image_run_args` parameter.
        """
        return image_run_args + parse_prefixed_args('-Dnative-image.benchmark.extra-run-arg=', args)

    def extra_agent_run_arg(self, benchmark, args, image_run_args):
        """Returns all arguments passed to the agent run.

        This includes those passed globally on the `mx benchmark` command line after the last `--`.
        These arguments are passed via the `image_run_args` parameter.
        Conflicting global arguments might be filtered out. The function `strip_args_with_number()` can help with that.
        """
        return image_run_args + parse_prefixed_args('-Dnative-image.benchmark.extra-agent-run-arg=', args)

    def extra_agentlib_options(self, benchmark, args, image_run_args):
        """Returns additional native-image-agent options.

        The returned options are added to the agentlib:native-image-agent option list.
        The config-output-dir is configured by the benchmark runner and cannot be overridden.
        """

        # All Renaissance Spark benchmarks require lambda class predefinition, so we need this additional option that
        # is used for the class predefinition feature. See GR-37506
        return ['experimental-class-define-support'] if (benchmark in ['chi-square', 'gauss-mix', 'movie-lens', 'page-rank']) else []

    def extra_profile_run_arg(self, benchmark, args, image_run_args, should_strip_run_args):
        """Returns all arguments passed to the profiling run.

        This includes those passed globally on the `mx benchmark` command line after the last `--`.
        These arguments are passed via the `image_run_args` parameter.
        Conflicting global arguments might be filtered out. The function `strip_args_with_number()` can help with that.
        """
        # either use extra profile run args if set or otherwise the extra run args
        extra_profile_run_args = parse_prefixed_args('-Dnative-image.benchmark.extra-profile-run-arg=', args) or parse_prefixed_args('-Dnative-image.benchmark.extra-run-arg=', args)
        return image_run_args + extra_profile_run_args

    def extra_agent_profile_run_arg(self, benchmark, args, image_run_args):
        """Returns all arguments passed to the agent profiling run.

        This includes those passed globally on the `mx benchmark` command line after the last `--`.
        These arguments are passed via the `image_run_args` parameter.
        Conflicting global arguments might be filtered out. The function `strip_args_with_number()` can help with that.
        """
        return image_run_args + parse_prefixed_args('-Dnative-image.benchmark.extra-agent-profile-run-arg=', args)

    def benchmark_output_dir(self, _, args):
        parsed_args = parse_prefixed_args('-Dnative-image.benchmark.benchmark-output-dir=', args)
        if parsed_args:
            return parsed_args[0]
        else:
            return None

    def stages(self, args):
        parsed_arg = parse_prefixed_arg('-Dnative-image.benchmark.stages=', args, 'Native Image benchmark stages should only be specified once.')
        return parsed_arg.split(',') if parsed_arg else self.default_stages()

    def default_stages(self) -> List[str]:
        """Default list of stages to run if none have been specified."""
        return ["agent", "instrument-image", "instrument-run", "image", "run"]

    def skip_agent_assertions(self, _, args):
        parsed_args = parse_prefixed_args('-Dnative-image.benchmark.skip-agent-assertions=', args)
        if 'true' in parsed_args or 'True' in parsed_args:
            return True
        elif 'false' in parsed_args or 'False' in parsed_args:
            return False
        else:
            return None

    def build_assertions(self, benchmark, is_gate):
        # We are skipping build assertions when a benchmark is not a part of a gate.
        return ['-J-ea', '-J-esa'] if is_gate else []

    # Override and return False if this suite should not check for samples in runs with PGO
    def checkSamplesInPgo(self):
        return True


def measureTimeToFirstResponse(bmSuite):
    protocolHost = bmSuite.serviceHost()
    servicePath = bmSuite.serviceEndpoint()
    if not (protocolHost.startswith('http') or protocolHost.startswith('https')):
        protocolHost = "http://" + protocolHost
    if not (servicePath.startswith('/') or protocolHost.endswith('/')):
        servicePath = '/' + servicePath
    url = "{}:{}{}".format(protocolHost, bmSuite.servicePort(), servicePath)

    measurementStartTime = time.time()
    sentRequests = 0
    receivedNon200Responses = 0
    last_report_time = time.time()
    req = urllib.request.Request(url, headers=bmSuite.requestHeaders())
    while time.time() - measurementStartTime < 120:
        time.sleep(.0001)
        if sentRequests > 0 and time.time() - last_report_time > 10:
            last_report_time = time.time()
            mx.log("Sent {:d} requests so far but did not receive a response with code 200 yet.".format(sentRequests))

        try:
            sentRequests += 1
            res = urllib.request.urlopen(req, timeout=10)
            responseCode = res.getcode()
            if responseCode == 200:
                processStartTime = mx.get_last_subprocess_start_time()
                finishTime = datetime.datetime.now()
                msToFirstResponse = (finishTime - processStartTime).total_seconds() * 1000
                currentOutput = "First response received in {} ms".format(msToFirstResponse)
                bmSuite.timeToFirstResponseOutputs.append(currentOutput)
                mx.log(currentOutput)
                return
            else:
                if receivedNon200Responses < 10:
                    mx.log("Received a response but it had response code " + str(responseCode) + " instead of 200")
                elif receivedNon200Responses == 10:
                    mx.log("No more response codes will be printed (already printed 10 response codes)")
                receivedNon200Responses += 1
        except IOError:
            pass

    mx.abort("Failed to measure time to first response. Service not reachable at " + url)


class BaseMicroserviceBenchmarkSuite(mx_benchmark.JavaBenchmarkSuite, NativeImageBenchmarkMixin):
    """
    Base class for Microservice benchmark suites. A Microservice is an application that opens a port that is ready to
    receive requests. This benchmark suite runs a tester process in the background (such as Wrk2) and run a
    Microservice application in foreground. Once the tester finishes stress testing the application, the tester process
    terminates and the application is killed with SIGTERM.

    The number of environment variables affects the startup time of all microservice frameworks. To ensure benchmark
    stability, we therefore execute those benchmarks with an empty set of environment variables.
    """

    NumMeasureTimeToFirstResponse = 10

    def __init__(self):
        super(BaseMicroserviceBenchmarkSuite, self).__init__()
        self.timeToFirstResponseOutputs = []
        self.startupOutput = ''
        self.peakOutput = ''
        self.latencyOutput = ''
        self.bmSuiteArgs = None
        self.workloadPath = None
        self.measureLatency = None
        self.measureFirstResponse = None
        self.measureStartup = None
        self.measurePeak = None
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument("--workload-configuration", type=str, default=None, help="Path to workload configuration.")
        self.parser.add_argument("--skip-latency-measurements", action='store_true', help="Determines if the latency measurements should be skipped.")
        self.parser.add_argument("--skip-first-response-measurements", action='store_true', help="Determines if the time-to-first-response measurements should be skipped.")
        self.parser.add_argument("--skip-startup-measurements", action='store_true', help="Determines if the startup performance measurements should be skipped.")
        self.parser.add_argument("--skip-peak-measurements", action='store_true', help="Determines if the peak performance measurements should be skipped.")

    def benchMicroserviceName(self):
        """
        Returns the microservice name. The convention here is that the benchmark name contains two elements separated
        by a hyphen ('-'):
        - the microservice name (shopcart, for example);
        - the tester tool name (wrk, for example).

        :return: Microservice name.
        :rtype: str
        """

        if len(self.benchSuiteName().split('-', 1)) < 2:
            mx.abort("Invalid benchmark suite name: " + self.benchSuiteName())
        return self.benchSuiteName().split("-", 1)[0]

    def validateReturnCode(self, retcode):
        return retcode == 143

    def defaultWorkloadPath(self, benchmarkName):
        """Returns the workload configuration path.

        :return: Path to configuration file.
        :rtype: str
        """
        raise NotImplementedError()

    def workloadConfigurationPath(self):
        if self.workloadPath:
            mx.log("Using user-provided workload configuration file: {0}".format(self.workloadPath))
            return self.workloadPath
        else:
            return self.defaultWorkloadPath(self.benchmarkName())

    def applicationPath(self):
        """Returns the application Jar path.

        :return: Path to Jar.
        :rtype: str
        """
        raise NotImplementedError()

    def serviceHost(self):
        """Returns the microservice host.

        :return: Host used to access the microservice.
        :rtype: str
        """
        return 'localhost'

    def servicePort(self):
        """Returns the microservice port.

        :return: Port that the microservice is using to receive requests.
        :rtype: int
        """
        return 8080

    def serviceEndpoint(self):
        """Returns the microservice path that checks if the service is running.

        :return: service path
        :rtype: str
        """
        return ''

    def requestHeaders(self):
        """Returns extra headers to be sent when markign requests to the service endpoint..
        :rtype: dict[str, str]
        """
        return {}

    def inNativeMode(self):
        return "native-image" in self.jvm(self.bmSuiteArgs)

    def createCommandLineArgs(self, benchmarks, bmSuiteArgs):
        return self.vmArgs(bmSuiteArgs) + ["-jar", self.applicationPath()]

    @staticmethod
    def waitForPort(port, timeout=60):
        try:
            import psutil
        except ImportError:
            # Note: abort fails to find the process (not registered yet in mx) if we are too fast failing here.
            time.sleep(5)
            mx.abort("Failed to import {0} dependency module: psutil".format(BaseMicroserviceBenchmarkSuite.__name__))
        for _ in range(timeout + 1):
            for proc in psutil.process_iter():
                try:
                    for conns in proc.connections(kind='inet'):
                        if conns.laddr.port == port:
                            return proc
                except:
                    pass
            time.sleep(1)
        return None

    def runAndReturnStdOut(self, benchmarks, bmSuiteArgs):
        ret_code, applicationOutput, dims = super(BaseMicroserviceBenchmarkSuite, self).runAndReturnStdOut(benchmarks, bmSuiteArgs)
        result = ret_code, "\n".join(self.timeToFirstResponseOutputs) + '\n' + self.startupOutput + '\n' + self.peakOutput + '\n' + self.latencyOutput + '\n' + applicationOutput, dims

        # For HotSpot, the rules are executed after every execution. So, it is necessary to reset the data to avoid duplication of datapoints.
        if not self.inNativeMode():
            self.timeToFirstResponseOutputs = []
            self.startupOutput = ''
            self.peakOutput = ''
            self.latencyOutput = ''

        return result

    @staticmethod
    def terminateApplication(port):
        proc = BaseMicroserviceBenchmarkSuite.waitForPort(port, 0)
        if proc:
            proc.send_signal(signal.SIGTERM)
            return True
        else:
            return False

    @staticmethod
    def testTimeToFirstResponseInBackground(benchmarkSuite):
        mx.log("--------------------------------------------")
        mx.log("Started time-to-first-response measurements.")
        mx.log("--------------------------------------------")
        measureTimeToFirstResponse(benchmarkSuite)
        if not BaseMicroserviceBenchmarkSuite.waitForPort(benchmarkSuite.servicePort()):
            mx.abort("Failed to find server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))
        if not BaseMicroserviceBenchmarkSuite.terminateApplication(benchmarkSuite.servicePort()):
            mx.abort("Failed to terminate server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))

    @staticmethod
    def testStartupPerformanceInBackground(benchmarkSuite):
        mx.log("-----------------------------------------")
        mx.log("Started startup performance measurements.")
        mx.log("-----------------------------------------")
        if not BaseMicroserviceBenchmarkSuite.waitForPort(benchmarkSuite.servicePort()):
            mx.abort("Failed to find server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))
        benchmarkSuite.testStartupPerformance()
        if not BaseMicroserviceBenchmarkSuite.terminateApplication(benchmarkSuite.servicePort()):
            mx.abort("Failed to terminate server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))

    @staticmethod
    def testPeakPerformanceInBackground(benchmarkSuite, warmup=True):
        mx.log("--------------------------------------")
        mx.log("Started peak performance measurements.")
        mx.log("--------------------------------------")
        if not BaseMicroserviceBenchmarkSuite.waitForPort(benchmarkSuite.servicePort()):
            mx.abort("Failed to find server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))
        benchmarkSuite.testPeakPerformance(warmup)
        if not BaseMicroserviceBenchmarkSuite.terminateApplication(benchmarkSuite.servicePort()):
            mx.abort("Failed to terminate server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))

    @staticmethod
    def calibrateLatencyTestInBackground(benchmarkSuite):
        mx.log("---------------------------------------------")
        mx.log("Started calibration for latency measurements.")
        mx.log("---------------------------------------------")
        if not BaseMicroserviceBenchmarkSuite.waitForPort(benchmarkSuite.servicePort()):
            mx.abort("Failed to find server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))
        benchmarkSuite.calibrateLatencyTest()
        if not BaseMicroserviceBenchmarkSuite.terminateApplication(benchmarkSuite.servicePort()):
            mx.abort("Failed to terminate server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))

    @staticmethod
    def testLatencyInBackground(benchmarkSuite):
        mx.log("-----------------------------")
        mx.log("Started latency measurements.")
        mx.log("-----------------------------")
        if not BaseMicroserviceBenchmarkSuite.waitForPort(benchmarkSuite.servicePort()):
            mx.abort("Failed to find server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))
        benchmarkSuite.testLatency()
        if not BaseMicroserviceBenchmarkSuite.terminateApplication(benchmarkSuite.servicePort()):
            mx.abort("Failed to terminate server application in {0}".format(BaseMicroserviceBenchmarkSuite.__name__))

    def get_env(self):
        return {}

    def run_stage(self, vm, stage, server_command, out, err, cwd, nonZeroIsFatal):
        if 'image' in stage:
            # For image stages, we just run the given command
            return super(BaseMicroserviceBenchmarkSuite, self).run_stage(vm, stage, server_command, out, err, cwd, nonZeroIsFatal)
        else:
            if stage == 'run':
                serverCommandWithTracker = self.apply_command_mapper_hooks(server_command, vm)

                mx_benchmark.disable_tracker()
                serverCommandWithoutTracker = self.apply_command_mapper_hooks(server_command, vm)
                mx_benchmark.enable_tracker()

                # Measure time-to-first-response multiple times (without any command mapper hooks as those affect the measurement significantly)
                for _ in range(self.NumMeasureTimeToFirstResponse):
                    with EmptyEnv(self.get_env()):
                        measurementThread = self.startDaemonThread(target=BaseMicroserviceBenchmarkSuite.testTimeToFirstResponseInBackground, args=[self])
                        returnCode = mx.run(server_command, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)
                        measurementThread.join()
                    if not self.validateReturnCode(returnCode):
                        mx.abort("The server application unexpectedly ended with return code " + str(returnCode))

                # Measure startup performance (without RSS tracker)
                with EmptyEnv(self.get_env()):
                    measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testStartupPerformanceInBackground, [self])
                    returnCode = mx.run(serverCommandWithoutTracker, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)
                    measurementThread.join()
                if not self.validateReturnCode(returnCode):
                    mx.abort("The server application unexpectedly ended with return code " + str(returnCode))

                # Measure peak performance (with all command mapper hooks)
                with EmptyEnv(self.get_env()):
                    measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testPeakPerformanceInBackground, [self])
                    returnCode = mx.run(serverCommandWithTracker, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)
                    measurementThread.join()
                if not self.validateReturnCode(returnCode):
                    mx.abort("The server application unexpectedly ended with return code " + str(returnCode))

                if self.measureLatency:
                    if not any([c.get("requests-per-second") for c in self.loadConfiguration("latency")]):
                        # Calibrate for latency measurements (without RSS tracker) if no fixed request rate has been provided in the config
                        with EmptyEnv(self.get_env()):
                            measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.calibrateLatencyTestInBackground, [self])
                            returnCode = mx.run(serverCommandWithoutTracker, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)
                            measurementThread.join()
                        if not self.validateReturnCode(returnCode):
                            mx.abort("The server application unexpectedly ended with return code " + str(returnCode))

                    # Measure latency (without RSS tracker)
                    with EmptyEnv(self.get_env()):
                        measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testLatencyInBackground, [self])
                        returnCode = mx.run(serverCommandWithoutTracker, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)
                        measurementThread.join()
                    if not self.validateReturnCode(returnCode):
                        mx.abort("The server application unexpectedly ended with return code " + str(returnCode))

                return returnCode
            elif stage == 'agent' or 'instrument-run' in stage:
                # For the agent and the instrumented run, it is sufficient to run the peak performance workload.
                with EmptyEnv(self.get_env()):
                    measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testPeakPerformanceInBackground, [self, False])
                    returnCode = mx.run(server_command, out=out, err=err, cwd=cwd, nonZeroIsFatal=nonZeroIsFatal)
                    measurementThread.join()
                return returnCode
            else:
                mx.abort("Unexpected stage: " + stage)

    def startDaemonThread(self, target, args):
        def true_target(*true_target_args):
            self.setup_application()
            target(*args)

        thread = threading.Thread(target=true_target)
        thread.setDaemon(True)
        thread.start()
        return thread

    def setup_application(self):
        pass

    def rules(self, output, benchmarks, bmSuiteArgs):
        return [
            mx_benchmark.StdOutRule(
                r"First response received in (?P<firstResponse>\d*[.,]?\d*) ms",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "time-to-first-response",
                    "metric.value": ("<firstResponse>", float),
                    "metric.unit": "ms",
                    "metric.better": "lower",
                }
            )
        ]


    def computePeakThroughputRSS(self, datapoints):
        tputDatapoint = None
        rssDatapoint = None
        for datapoint in datapoints:
            if datapoint['metric.name'] == 'peak-throughput':
                tputDatapoint = datapoint
            if datapoint['metric.name'] == 'max-rss':
                rssDatapoint = datapoint
        if tputDatapoint and rssDatapoint:
            newdatapoint = copy.deepcopy(tputDatapoint)
            newdatapoint['metric.name'] = 'ops-per-GB-second'
            newtput = convertValue(tputUnitTable, float(tputDatapoint['metric.value']), tputDatapoint['metric.unit'], "op/s")
            newrss = convertValue(memUnitTable, float(rssDatapoint['metric.value']), rssDatapoint['metric.unit'], "GB")
            newdatapoint['metric.value'] = newtput / newrss
            newdatapoint['metric.unit'] = 'op/GB*s'
            newdatapoint['metric.better'] = 'higher'
            return newdatapoint
        else:
            return None

    def validateStdoutWithDimensions(self, out, benchmarks, bmSuiteArgs, retcode=None, dims=None, extraRules=None):
        datapoints = super(BaseMicroserviceBenchmarkSuite, self).validateStdoutWithDimensions(
            out=out, benchmarks=benchmarks, bmSuiteArgs=bmSuiteArgs, retcode=retcode, dims=dims, extraRules=extraRules)

        newdatapoint = self.computePeakThroughputRSS(datapoints)
        if newdatapoint:
            datapoints.append(newdatapoint)

        return datapoints

    def run(self, benchmarks, bmSuiteArgs):
        if len(benchmarks) > 1:
            mx.abort("A single benchmark should be specified for {0}.".format(BaseMicroserviceBenchmarkSuite.__name__))
        self.bmSuiteArgs = bmSuiteArgs
        self.benchmark_name = benchmarks[0]
        args, remainder = self.parser.parse_known_args(self.bmSuiteArgs)
        self.workloadPath = args.workload_configuration
        self.measureLatency = not args.skip_latency_measurements
        self.measureFirstResponse = not args.skip_first_response_measurements
        self.measureStartup = not args.skip_startup_measurements
        self.measurePeak = not args.skip_peak_measurements

        if not self.inNativeMode():
            datapoints = []
            if self.measureFirstResponse:
                # Measure time-to-first-response (without any command mapper hooks as those affect the measurement significantly)
                mx.disable_command_mapper_hooks()
                for _ in range(self.NumMeasureTimeToFirstResponse):
                    with EmptyEnv(self.get_env()):
                        measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testTimeToFirstResponseInBackground, [self])
                        datapoints += super(BaseMicroserviceBenchmarkSuite, self).run(benchmarks, remainder)
                        measurementThread.join()
                mx.enable_command_mapper_hooks()

            if self.measureStartup:
                # Measure startup performance (without RSS tracker)
                mx_benchmark.disable_tracker()
                with EmptyEnv(self.get_env()):
                    measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testStartupPerformanceInBackground, [self])
                    datapoints += super(BaseMicroserviceBenchmarkSuite, self).run(benchmarks, remainder)
                    measurementThread.join()
                mx_benchmark.enable_tracker()

            if self.measurePeak:
                # Measure peak performance (with all command mapper hooks)
                with EmptyEnv(self.get_env()):
                    measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testPeakPerformanceInBackground, [self])
                    datapoints += super(BaseMicroserviceBenchmarkSuite, self).run(benchmarks, remainder)
                    measurementThread.join()

            if self.measureLatency:
                if not [c.get("requests-per-second") for c in self.loadConfiguration("latency") if c.get("requests-per-second")]:
                    # Calibrate for latency measurements (without RSS tracker) if no fixed request rate has been provided in the config
                    mx_benchmark.disable_tracker()
                    with EmptyEnv(self.get_env()):
                        measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.calibrateLatencyTestInBackground, [self])
                        datapoints += super(BaseMicroserviceBenchmarkSuite, self).run(benchmarks, remainder)
                        measurementThread.join()

                # Measure latency (without RSS tracker)
                with EmptyEnv(self.get_env()):
                    measurementThread = self.startDaemonThread(BaseMicroserviceBenchmarkSuite.testLatencyInBackground, [self])
                    datapoints += super(BaseMicroserviceBenchmarkSuite, self).run(benchmarks, remainder)
                    measurementThread.join()
                mx_benchmark.enable_tracker()

            return datapoints
        else:
            return super(BaseMicroserviceBenchmarkSuite, self).run(benchmarks, remainder)


class NativeImageBundleBasedBenchmarkMixin(object):
    def applicationDist(self):
        raise NotImplementedError()

    def uses_bundles(self):
        raise NotImplementedError()

    def _get_single_file_with_extension_from_dist(self, extension):
        lib = self.applicationDist()
        matching_files = [filename for filename in os.listdir(lib) if filename.endswith(extension)]
        assert len(matching_files) == 1, f"When using bundle support, the benchmark must contain a single file with extension {extension} in its mx library"
        matching_file = os.path.join(lib, matching_files[0])
        return matching_file

    def create_bundle_command_line_args(self, benchmarks, bmSuiteArgs):
        assert self.uses_bundles()
        executable_jar = self._get_single_file_with_extension_from_dist(".jar")
        return self.vmArgs(bmSuiteArgs) + ["-jar", executable_jar]

    def create_bundle_image_build_arguments(self):
        if self.uses_bundles():
            return [f'--bundle-apply={self._get_single_file_with_extension_from_dist(".nib")}']
        return []


class EmptyEnv:
    def __init__(self, env):
        self.env = env

    def __enter__(self):
        self._prev_environ = os.environ
        os.environ = self.env.copy()
        # urllib.request caches http_proxy, https_proxy etc. globally but doesn't cache no_proxy
        # preserve no_proxy to avoid issues with proxies
        if 'no_proxy' in self._prev_environ:
            os.environ['no_proxy'] = self._prev_environ['no_proxy']
        if 'NO_PROXY' in self._prev_environ:
            os.environ['NO_PROXY'] = self._prev_environ['NO_PROXY']

    def __exit__(self, exc_type, exc_val, exc_tb):
        os.environ = self._prev_environ

class BaseJMeterBenchmarkSuite(BaseMicroserviceBenchmarkSuite, mx_benchmark.AveragingBenchmarkMixin):
    """Base class for JMeter based benchmark suites."""

    def jmeterVersion(self):
        return '5.3'

    def rules(self, out, benchmarks, bmSuiteArgs):
        # Example of jmeter output (time = 100s):
        #
        # summary +     59 in 00:00:10 =    5.9/s Avg:   449 Min:    68 Max:  7725 Err:     0 (0.00%) Active: 3 Started: 3 Finished: 0
        # summary +   1945 in 00:00:30 =   64.9/s Avg:    46 Min:    26 Max:   116 Err:     0 (0.00%) Active: 3 Started: 3 Finished: 0
        # summary =   2004 in 00:00:40 =   50.2/s Avg:    57 Min:    26 Max:  7725 Err:     0 (0.00%)
        # summary +   1906 in 00:00:30 =   63.6/s Avg:    47 Min:    26 Max:    73 Err:     0 (0.00%) Active: 3 Started: 3 Finished: 0
        # summary =   3910 in 00:01:10 =   55.9/s Avg:    52 Min:    26 Max:  7725 Err:     0 (0.00%)
        # summary +   1600 in 00:00:30 =   53.3/s Avg:    56 Min:    29 Max:    71 Err:     0 (0.00%) Active: 3 Started: 3 Finished: 0
        # summary =   5510 in 00:01:40 =   55.1/s Avg:    53 Min:    26 Max:  7725 Err:     0 (0.00%)
        # summary +      8 in 00:00:00 =   68.4/s Avg:    46 Min:    30 Max:    58 Err:     0 (0.00%) Active: 0 Started: 3 Finished: 3
        # summary =   5518 in 00:01:40 =   55.1/s Avg:    53 Min:    26 Max:  7725 Err:     0 (0.00%)
        #
        # The following rules matches `^summary \+` and reports the corresponding data points as 'warmup'.
        # Note that the `run()` function calls `addAverageAcrossLatestResults()`, which computes
        # the avg. of the last `AveragingBenchmarkMixin.getExtraIterationCount()` warmup data points
        # and reports that value as 'throughput'.
        pattern = r"^summary \+\s+(?P<requests>[0-9]+) in (?P<hours>\d+):(?P<minutes>\d\d):(?P<seconds>\d\d) =\s+(?P<throughput>\d*[.,]?\d*)/s Avg:\s+(?P<avg>\d+) Min:\s+(?P<min>\d+) Max:\s+(?P<max>\d+) Err:\s+(?P<errors>\d+) \((?P<errpct>\d*[.,]?\d*)\%\)"  # pylint: disable=line-too-long
        return [
            mx_benchmark.StdOutRule(
                pattern,
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "warmup",
                    "metric.value": ("<throughput>", float),
                    "metric.unit": "op/s",
                    "metric.better": "higher",
                    "metric.iteration": ("$iteration", int),
                    "warnings": ("<errors>", str),
                }
            ),
            mx_benchmark.StdOutRule(
                pattern,
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "peak-latency",
                    "metric.value": ("<max>", float),
                    "metric.unit": "ms",
                    "metric.better": "lower",
                    "metric.iteration": ("$iteration", int),
                    "warnings": ("<errors>", str),
                }
            )
        ] + super(BaseJMeterBenchmarkSuite, self).rules(out, benchmarks, bmSuiteArgs)

    def testStartupPerformance(self):
        self.startupOutput = ''

    def testPeakPerformance(self, warmup):
        jmeterDirectory = mx.library("APACHE_JMETER_" + self.jmeterVersion(), True).get_path(True)
        jmeterPath = os.path.join(jmeterDirectory, "apache-jmeter-" + self.jmeterVersion(), "bin/ApacheJMeter.jar")
        extraVMArgs = []
        if mx.get_jdk(tag='default').javaCompliance >= '9':
            extraVMArgs += ["--add-opens=java.desktop/sun.awt=ALL-UNNAMED",
                            "--add-opens=java.desktop/sun.swing=ALL-UNNAMED",
                            "--add-opens=java.desktop/javax.swing.text.html=ALL-UNNAMED",
                            "--add-opens=java.desktop/java.awt=ALL-UNNAMED",
                            "--add-opens=java.desktop/java.awt.font=ALL-UNNAMED",
                            "--add-opens=java.desktop/sun.awt.X11=ALL-UNNAMED",
                            "--add-opens=java.base/java.lang=ALL-UNNAMED",
                            "--add-opens=java.base/java.lang.invoke=ALL-UNNAMED",
                            "--add-opens=java.base/java.lang.reflect=ALL-UNNAMED",
                            "--add-opens=java.base/java.util=ALL-UNNAMED",
                            "--add-opens=java.base/java.text=ALL-UNNAMED"]
        jmeterCmd = [mx.get_jdk(tag='default').java] + extraVMArgs + ["-jar", jmeterPath,
                                                         "-t", self.workloadConfigurationPath(),
                                                         "-n", "-j", "/dev/stdout"] + self.extraJMeterArgs()
        mx.log("Running JMeter: {0}".format(jmeterCmd))
        output = mx.TeeOutputCapture(mx.OutputCapture())
        mx.run(jmeterCmd, out=output, err=output)
        self.peakOutput = output.underlying.data

    def extraJMeterArgs(self):
        return []

    def calibrateLatencyTest(self):
        pass

    def testLatency(self):
        self.latencyOutput = ''

    def tailDatapointsToSkip(self, results):
        return int(len(results) * .10)

    def run(self, benchmarks, bmSuiteArgs):
        results = super(BaseJMeterBenchmarkSuite, self).run(benchmarks, bmSuiteArgs)
        results = results[:len(results) - self.tailDatapointsToSkip(results)]
        self.addAverageAcrossLatestResults(results, "throughput")
        return results

class BaseWrkBenchmarkSuite(BaseMicroserviceBenchmarkSuite):
    """Base class for Wrk based benchmark suites."""

    def loadConfiguration(self, groupKey):
        """Returns a json object that describes the Wrk configuration. The following syntax is expected:
        {
          "target-url" : <URL to target, for example "http://localhost:8080">,
          "connections" : <number of connections to keep open>,
          "threads" : <number of threads to use>,
          "throughput" : {
            "script" : <path to lua script to be used>,
            "warmup-requests-per-second" : <requests per second during the warmup run>,
            "warmup-duration" : <duration of the warmup run, for example "30s">,
            "duration" : <duration of the test, for example "30s">,
          },
          "latency" : {
            "script" : [<lua scripts that will be executed sequentially>],
            "warmup-requests-per-second" : [<requests per second during the warmup run (one entry per lua script)>],
            "warmup-duration" : [<duration of the warmup run (one entry per lua script)>],
            "requests-per-second" : [<requests per second during the run> (one entry per lua script)>],
            "duration" : [<duration of the test (one entry per lua script)>]
          }
        }

        All json fields are required.

        :return: Configuration json.
        :rtype: json
        """
        with open(self.workloadConfigurationPath()) as configFile:
            config = json.load(configFile)
            mx.log("Loading configuration file for {0}: {1}".format(BaseWrkBenchmarkSuite.__name__, configFile.name))

            targetUrl = self.readConfig(config, "target-url")
            connections = self.readConfig(config, "connections")
            threads = self.readConfig(config, "threads")

            group = self.readConfig(config, groupKey)
            script = self.readConfig(group, "script")
            warmupRequestsPerSecond = self.readConfig(group, "warmup-requests-per-second")
            warmupDuration = self.readConfig(group, "warmup-duration")
            requestsPerSecond = self.readConfig(group, "requests-per-second", optional=True)
            duration = self.readConfig(group, "duration")

            scalarScriptValue = self.isScalarValue(script)
            if scalarScriptValue != self.isScalarValue(warmupRequestsPerSecond) or scalarScriptValue != self.isScalarValue(warmupDuration) or scalarScriptValue != self.isScalarValue(duration):
                mx.abort("The configuration elements 'script', 'warmup-requests-per-second', 'warmup-duration', and 'duration' must have the same number of elements.")

            results = []
            if scalarScriptValue:
                result = {}
                result["target-url"] = targetUrl
                result["connections"] = connections
                result["threads"] = threads
                result["script"] = script
                result["warmup-requests-per-second"] = warmupRequestsPerSecond
                result["warmup-duration"] = warmupDuration
                result["duration"] = duration
                if requestsPerSecond:
                    result["requests-per-second"] = requestsPerSecond
                results.append(result)
            else:
                count = len(script)
                if count != len(warmupRequestsPerSecond) or count != len(warmupDuration) or count != len(duration):
                    mx.abort("The configuration elements 'script', 'warmup-requests-per-second', 'warmup-duration', and 'duration' must have the same number of elements.")

                for i in range(count):
                    result = {}
                    result["target-url"] = targetUrl
                    result["connections"] = connections
                    result["threads"] = threads
                    result["script"] = script[i]
                    result["warmup-requests-per-second"] = warmupRequestsPerSecond[i]
                    result["warmup-duration"] = warmupDuration[i]
                    result["duration"] = duration[i]
                    if requestsPerSecond:
                        result["requests-per-second"] = requestsPerSecond[i]
                    results.append(result)

            return results

    def readConfig(self, config, key, optional=False):
        if key in config:
            return config[key]
        elif optional:
            return None
        else:
            mx.abort(f"Mandatory entry {key} not specified in Wrk configuration.")

    def isScalarValue(self, value):
        return type(value) in (int, float, bool) or isinstance(value, ("".__class__, u"".__class__)) # pylint: disable=unidiomatic-typecheck

    def getScriptPath(self, config):
        return os.path.join(self.applicationDist(), "workloads", config["script"])

    def defaultWorkloadPath(self, benchmark):
        return os.path.join(self.applicationDist(), "workloads", benchmark + ".wrk")

    def testStartupPerformance(self):
        configs = self.loadConfiguration("throughput")
        if len(configs) != 1:
            mx.abort("Expected exactly one lua script in the throughput configuration.")

        # Measure throughput for 15 seconds without warmup.
        config = configs[0]
        wrkFlags = self.getStartupFlags(config)
        output = self.runWrk1(wrkFlags)
        self.startupOutput = self.writeWrk1Results('startup-throughput', 'startup-latency-co', output)

    def testPeakPerformance(self, warmup):
        configs = self.loadConfiguration("throughput")
        if len(configs) != 1:
            mx.abort("Expected exactly one lua script in the throughput configuration.")

        config = configs[0]
        if warmup:
            # Warmup with a fixed number of requests.
            wrkFlags = self.getWarmupFlags(config)
            warmupOutput = self.runWrk2(wrkFlags)
            self.verifyWarmup(warmupOutput, config)

        # Measure peak performance.
        wrkFlags = self.getThroughputFlags(config)
        peakOutput = self.runWrk1(wrkFlags)
        self.peakOutput = self.writeWrk1Results('peak-throughput', 'peak-latency-co', peakOutput)

    def calibrateLatencyTest(self):
        configs = self.loadConfiguration("latency")
        numScripts = len(configs)
        if numScripts < 1:
            mx.abort("Expected at least one lua script in the latency configuration.")

        for i in range(numScripts):
            # Warmup with a fixed number of requests.
            config = configs[i]
            wrkFlags = self.getWarmupFlags(config)
            warmupOutput = self.runWrk2(wrkFlags)
            self.verifyWarmup(warmupOutput, config)

        self.calibratedThroughput = []
        for i in range(numScripts):
            # Measure the maximum throughput.
            config = configs[i]
            wrkFlags = self.getThroughputFlags(config)
            throughputOutput = self.runWrk1(wrkFlags)
            self.calibratedThroughput.append(self.extractThroughput(throughputOutput))

    def testLatency(self):
        configs = self.loadConfiguration("latency")
        numScripts = len(configs)
        if numScripts < 1:
            mx.abort("Expected at least one lua script in the latency configuration.")

        for i in range(numScripts):
            # Warmup with a fixed number of requests.
            config = configs[i]
            wrkFlags = self.getWarmupFlags(config)
            warmupOutput = self.runWrk2(wrkFlags)
            self.verifyWarmup(warmupOutput, config)

        results = []
        for i in range(numScripts):
            # Measure latency using a constant rate (based on the previously measured max throughput).
            config = configs[i]
            if configs[i].get("requests-per-second"):
                expectedRate = configs[i]["requests-per-second"]
                mx.log(f"Using configured fixed throughput {expectedRate} ops/s for latency measurements.")
            else:
                expectedRate = int(self.calibratedThroughput[i] * 0.75)
                mx.log(f"Using dynamically computed throughput {expectedRate} ops/s for latency measurements (75% of max throughput).")
            wrkFlags = self.getLatencyFlags(config, expectedRate)
            constantRateOutput = self.runWrk2(wrkFlags)
            self.verifyThroughput(constantRateOutput, expectedRate)
            results.append(self.extractWrk2Results(constantRateOutput))

        self.latencyOutput = self.writeWrk2Results('throughput-for-peak-latency', 'peak-latency', results)

    def extractThroughput(self, output):
        matches = re.findall(r"^Requests/sec:\s*(\d*[.,]?\d*)\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            mx.abort("Expected exactly one throughput result in the output: " + str(matches))

        return float(matches[0])

    def extractWrk2Results(self, output):
        result = {}
        result["throughput"] = self.extractThroughput(output)

        matches = re.findall(r"^\s*(\d*[.,]?\d*%)\s+(\d*[.,]?\d*)([mun]?s)\s*$", output, re.MULTILINE)
        if len(matches) <= 0:
            mx.abort("No latency results found in output")

        for match in matches:
            val = convertValue(timeUnitTable, float(match[1]), match[2], 'ms')
            result[match[0]] = val

        return result

    def writeWrk2Results(self, throughputPrefix, latencyPrefix, results):
        average = self.computeAverage(results)

        output = []
        for key, value in average.items():
            if key == 'throughput':
                output.append("{} Requests/sec: {:f}".format(throughputPrefix, value))
            else:
                output.append("{} {} {:f}ms".format(latencyPrefix, key, value))

        return '\n'.join(output)

    def computeAverage(self, results):
        count = len(results)
        if count < 1:
            mx.abort("Expected at least one wrk2 result: " + str(count))
        elif count == 1:
            return results[0]

        average = results[0]
        averageKeys = set(average.keys())
        for i in range(1, count):
            result = results[i]
            if averageKeys != set(result.keys()):
                mx.abort("There is a mismatch between the keys of multiple wrk2 runs: " + str(averageKeys) + " vs. " + str(set(result.keys())))

            for key, value in result.items():
                average[key] += result[key]

        for key, value in average.items():
            average[key] = value / count

        return average

    def writeWrk1Results(self, throughputPrefix, latencyPrefix, output):
        result = []
        matches = re.findall(r"^Requests/sec:\s*\d*[.,]?\d*\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            mx.abort("Expected exactly one throughput result in the output: " + str(matches))

        result.append(throughputPrefix + " " + matches[0])

        matches = re.findall(r"^\s*(\d*[.,]?\d*%)\s+(\d*[.,]?\d*)([mun]?s)\s*$", output, re.MULTILINE)
        if len(matches) <= 0:
            mx.abort("No latency results found in output")

        for match in matches:
            val = convertValue(timeUnitTable, float(match[1]), match[2], 'ms')
            result.append(latencyPrefix + " {} {:f}ms".format(match[0], val))

        return '\n'.join(result)

    def verifyWarmup(self, output, config):
        expectedThroughput = float(config['warmup-requests-per-second'])
        self.verifyThroughput(output, expectedThroughput)

    def verifyThroughput(self, output, expectedThroughput):
        matches = re.findall(r"^Requests/sec:\s*(?P<throughput>\d*[.,]?\d*)\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            mx.abort("Expected exactly one throughput result in the output: " + str(matches))

        actualThroughput = float(matches[0])
        if actualThroughput < expectedThroughput * 0.97 or actualThroughput > expectedThroughput * 1.03:
            mx.warn("Throughput verification failed: expected requests/s: {:.2f}, actual requests/s: {:.2f}".format(expectedThroughput, actualThroughput))

    def runWrk1(self, wrkFlags):
        distro = self.getOS()
        arch = mx.get_arch()
        wrkDirectory = mx.library('WRK_MULTIARCH', True).get_path(True)
        wrkPath = os.path.join(wrkDirectory, "wrk-{os}-{arch}".format(os=distro, arch=arch))

        if not os.path.exists(wrkPath):
            raise ValueError("Unsupported OS or arch. Binary doesn't exist: {}".format(wrkPath))

        runWrkCmd = [wrkPath] + wrkFlags
        mx.log("Running Wrk: {0}".format(runWrkCmd))
        output = mx.TeeOutputCapture(mx.OutputCapture())
        mx.run(runWrkCmd, out=output, err=output)
        return output.underlying.data

    def runWrk2(self, wrkFlags):
        distro = self.getOS()
        arch = mx.get_arch()
        wrkDirectory = mx.library('WRK2_MULTIARCH', True).get_path(True)
        wrkPath = os.path.join(wrkDirectory, "wrk-{os}-{arch}".format(os=distro, arch=arch))

        if not os.path.exists(wrkPath):
            raise ValueError("Unsupported OS or arch. Binary doesn't exist: {}".format(wrkPath))

        runWrkCmd = [wrkPath] + wrkFlags
        mx.log("Running Wrk2: {0}".format(runWrkCmd))
        output = mx.TeeOutputCapture(mx.OutputCapture())
        mx.run(runWrkCmd, out=output, err=output)
        return output.underlying.data

    def getStartupFlags(self, config):
        wrkFlags = ['--duration', '15']
        wrkFlags += self.getWrkFlags(config, True)
        return wrkFlags

    def getWarmupFlags(self, config):
        wrkFlags = []
        wrkFlags += ['--duration', str(config['warmup-duration'])]
        wrkFlags += ['--rate', str(config['warmup-requests-per-second'])]
        wrkFlags += self.getWrkFlags(config, False)
        return wrkFlags

    def getThroughputFlags(self, config):
        wrkFlags = []
        wrkFlags += ['--duration', str(config['duration'])]
        wrkFlags += self.getWrkFlags(config, True)
        return wrkFlags

    def getLatencyFlags(self, config, rate):
        wrkFlags = ['--rate', str(rate)]
        wrkFlags += self.getThroughputFlags(config)
        return wrkFlags

    def getWrkFlags(self, config, latency):
        args = []
        if latency:
            args += ['--latency']

        args += ['--connections', str(config['connections'])]
        args += ['--threads', str(config['threads'])]
        args += ['--script', str(self.getScriptPath(config))]
        args.append(str(config['target-url']))
        args += ['--', str(config['threads'])]
        return args

    def getOS(self):
        if mx.get_os() == 'linux':
            return 'linux'
        elif mx.get_os() == 'darwin':
            return 'macos'
        else:
            mx.abort("{0} not supported in {1}.".format(BaseWrkBenchmarkSuite.__name__, mx.get_os()))

    def rules(self, out, benchmarks, bmSuiteArgs):
        # Example of wrk output:
        # "Requests/sec:   5453.61"
        return [
            mx_benchmark.StdOutRule(
                r"^startup-throughput Requests/sec:\s*(?P<throughput>\d*[.,]?\d*)\s*$",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "startup-throughput",
                    "metric.value": ("<throughput>", float),
                    "metric.unit": "op/s",
                    "metric.better": "higher",
                }
            ),
            mx_benchmark.StdOutRule(
                r"^peak-throughput Requests/sec:\s*(?P<throughput>\d*[.,]?\d*)\s*$",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "peak-throughput",
                    "metric.value": ("<throughput>", float),
                    "metric.unit": "op/s",
                    "metric.better": "higher",
                }
            ),
            mx_benchmark.StdOutRule(
                r"^throughput-for-peak-latency Requests/sec:\s*(?P<throughput>\d*[.,]?\d*)\s*$",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "throughput-for-peak-latency",
                    "metric.value": ("<throughput>", float),
                    "metric.unit": "op/s",
                    "metric.better": "higher",
                }
            ),
            mx_benchmark.StdOutRule(
                r"^startup-latency-co\s+(?P<percentile>\d*[.,]?\d*)%\s+(?P<latency>\d*[.,]?\d*)(?P<unit>ms)\s*$",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "startup-latency-co",
                    "metric.value": ("<latency>", float),
                    "metric.unit": ("ms", str),
                    "metric.better": "lower",
                    "metric.percentile": ("<percentile>", float),
                }
            ),
            mx_benchmark.StdOutRule(
                r"^peak-latency-co\s+(?P<percentile>\d*[.,]?\d*)%\s+(?P<latency>\d*[.,]?\d*)(?P<unit>ms)\s*$",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "peak-latency-co",
                    "metric.value": ("<latency>", float),
                    "metric.unit": ("ms", str),
                    "metric.better": "lower",
                    "metric.percentile": ("<percentile>", float),
                }
            ),
            mx_benchmark.StdOutRule(
                r"^peak-latency\s+(?P<percentile>\d*[.,]?\d*)%\s+(?P<latency>\d*[.,]?\d*)(?P<unit>ms)\s*$",
                {
                    "benchmark": benchmarks[0],
                    "bench-suite": self.benchSuiteName(),
                    "metric.name": "peak-latency",
                    "metric.value": ("<latency>", float),
                    "metric.unit": ("ms", str),
                    "metric.better": "lower",
                    "metric.percentile": ("<percentile>", float),
                }
            )
        ] + super(BaseWrkBenchmarkSuite, self).rules(out, benchmarks, bmSuiteArgs)
