# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache License Version 2.0.
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2017 Datadog, Inc.

import gzip
import json
import os
import re
import time
import urllib
import urllib2
from base64 import b64decode
from StringIO import StringIO

import boto3

# retrieve datadog options from KMS
KMS_ENCRYPTED_KEYS = os.environ['kmsEncryptedKeys']
kms = boto3.client('kms')
datadog_keys = json.loads(kms.decrypt(CiphertextBlob=b64decode(KMS_ENCRYPTED_KEYS))['Plaintext'])

print 'INFO Lambda function initialized, ready to send metrics'


def _process_rds_enhanced_monitoring_message(ts, message, account, region):
    instance_id = message["instanceID"]
    host_id = message["instanceResourceID"]
    tags = [
        'dbinstanceidentifier:%s' % instance_id,
        'aws_account:%s' % account,
        'engine:%s' % message["engine"],
    ]

    # metrics generation

    # uptime: "54 days, 1:53:04" to be converted into seconds
    uptime = 0
    uptime_msg = re.split(' days?, ', message["uptime"])  # edge case "1 day 1:53:04"
    if len(uptime_msg) == 2:
        uptime += 24 * 3600 * int(uptime_msg[0])
    uptime_day = uptime_msg[-1].split(':')
    uptime += 3600 * int(uptime_day[0])
    uptime += 60 * int(uptime_day[1])
    uptime += int(uptime_day[2])
    stats.gauge(
        'aws.rds.uptime', uptime, timestamp=ts, tags=tags, host=host_id
    )

    stats.gauge(
        'aws.rds.virtual_cpus', message["numVCPUs"], timestamp=ts, tags=tags, host=host_id
    )

    stats.gauge(
        'aws.rds.load.1', message["loadAverageMinute"]["one"],
        timestamp=ts, tags=tags, host=host_id
    )
    stats.gauge(
        'aws.rds.load.5', message["loadAverageMinute"]["five"],
        timestamp=ts, tags=tags, host=host_id
    )
    stats.gauge(
        'aws.rds.load.15', message["loadAverageMinute"]["fifteen"],
        timestamp=ts, tags=tags, host=host_id
    )

    for namespace in ["cpuUtilization", "memory", "tasks", "swap"]:
        for key, value in message.get(namespace, {}).iteritems():
            stats.gauge(
                'aws.rds.%s.%s' % (namespace.lower(), key), value,
                timestamp=ts, tags=tags, host=host_id
            )

    for network_stats in message.get("network", []):
        network_tag = ["interface:%s" % network_stats.pop("interface")]
        for key, value in network_stats.iteritems():
            stats.gauge(
                'aws.rds.network.%s' % key, value,
                timestamp=ts, tags=tags + network_tag, host=host_id
            )

    disk_stats = message.get("diskIO", [{}])[0]  # we never expect to have more than one disk
    for key, value in disk_stats.iteritems():
        stats.gauge(
            'aws.rds.diskio.%s' % key, value,
            timestamp=ts, tags=tags, host=host_id
        )

    for fs_stats in message.get("fileSys", []):
        fs_tag = [
            "name:%s" % fs_stats.pop("name"),
            "mountPoint:%s" % fs_stats.pop("mountPoint")
        ]
        for key, value in fs_stats.iteritems():
            stats.gauge(
                'aws.rds.filesystem.%s' % key, value,
                timestamp=ts, tags=tags + fs_tag, host=host_id
            )

    for process_stats in message.get("processList", []):
        process_tag = [
            "name:%s" % process_stats.pop("name"),
            "id:%s" % process_stats.pop("id")
        ]
        for key, value in process_stats.iteritems():
            stats.gauge(
                'aws.rds.process.%s' % key, value,
                timestamp=ts, tags=tags + process_tag, host=host_id
            )


def lambda_handler(event, context):
    ''' Process a RDS enhenced monitoring DATA_MESSAGE,
        coming from CLOUDWATCH LOGS
    '''
    # event is a dict containing a base64 string gzipped
    event = event['awslogs']['data']
    event = json.loads(
        gzip.GzipFile(fileobj=StringIO(event.decode('base64'))).read()
    )

    account = event['owner']
    region = context.invoked_function_arn.split(':', 4)[3]

    log_events = event['logEvents']

    for log_event in log_events:
        message = json.loads(log_event['message'])
        ts = log_event['timestamp'] / 1000
        _process_rds_enhanced_monitoring_message(ts, message, account, region)

    stats.flush()
    return {'Status': 'OK'}


# Helpers to send data to Datadog, inspired from https://github.com/DataDog/datadogpy

class Stats(object):

    def __init__(self):
        self.series = []

    def gauge(self, metric, value, timestamp=None, tags=None, host=None):
        base_dict = {
            'metric': metric,
            'points': [(int(timestamp or time.time()), value)],
            'type': 'gauge',
            'tags': tags,
        }
        if host:
            base_dict.update({'host': host})
        self.series.append(base_dict)

    def flush(self):
        metrics_dict = {
            'series': self.series,
        }
        self.series = []

        creds = urllib.urlencode(datadog_keys)
        data = json.dumps(metrics_dict)
        url = '%s?%s' % (datadog_keys.get('api_host', 'https://app.datadoghq.com/api/v1/series'), creds)
        req = urllib2.Request(url, data, {'Content-Type': 'application/json'})
        response = urllib2.urlopen(req)
        print 'INFO Submitted data with status', response.getcode()

stats = Stats()
