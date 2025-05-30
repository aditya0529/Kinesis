from constructs import Construct
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_s3 as s3,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_events as events,
    aws_events_targets as targets,
    aws_ecr as ecr,
    aws_ec2 as ec2,
    aws_certificatemanager as cert,
    aws_logs as logs,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_route53 as route53,
    aws_fis as fis,
    aws_synthetics as synthetics,
)

from util.app_config import ApplicationConfig

class cloud_infra(Stack):

    def create_canary_security_group(self, config, vpc):

        # security group
        sg = ec2.SecurityGroup(
            self,
            id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-sg-{config['resource_suffix']}",
            security_group_name=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-sg-{config['resource_suffix']}",
            description="Allow the communication from Canary",
            allow_all_outbound=False,
            # if this is set to false then no egress rule will be automatically created
            vpc=vpc
        )

        sg.add_egress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(443)
        )

        s3_prefix_list = ec2.PrefixList.from_prefix_list_id(
            self, id="S3PrefixList",
            prefix_list_id=config['s3_prefix_list']
        )

        sg.add_egress_rule(
            ec2.Peer.prefix_list(s3_prefix_list.prefix_list_id),
            ec2.Port.tcp(443)
        )

        return sg

    def create_artifact_store(self, config) -> s3.Bucket:

        bucket = s3.Bucket(self,
                           id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-s3-{config['resource_suffix']}",
                           bucket_name=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-s3-{config['workload_account']}-{config['resource_suffix']}",
                           block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                           encryption=s3.BucketEncryption.S3_MANAGED,
                           minimum_tls_version=1.2,
                           object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
                           object_lock_enabled=False,
                           enforce_ssl=True,
                           versioned=True,
                           lifecycle_rules=[s3.LifecycleRule(
                               enabled=True,
                               id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-s3-lifecycle-{config['resource_suffix']}",
                               noncurrent_version_expiration=Duration.days(
                                   7),
                               noncurrent_versions_to_retain=1
                           )
                           ]
                           )

        return bucket

    #create canary role
    def create_canary_role(self, config, bucket_name) -> iam.Role:
        canary_role = iam.Role(self,
                               id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-role-{config['resource_suffix']}",
                               assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'),
                               role_name=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-role-{config['resource_suffix']}",
                               managed_policies=[
                                   iam.ManagedPolicy.from_aws_managed_policy_name('CloudWatchSyntheticsFullAccess'),
                                   iam.ManagedPolicy.from_aws_managed_policy_name('AmazonEC2FullAccess')
                               ]
                               )

        canary_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:PutObject",
                    "s3:GetObject"
                ],
                resources=[
                    f"arn:aws:s3:::{bucket_name}/*"
                ]
            )
        )

        canary_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:GetBucketLocation"
                ],
                resources=[
                    f"arn:aws:s3:::{bucket_name}"
                ]
            )
        )

        canary_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:CreateLogGroup"
                ],
                resources=[
                    f"arn:aws:logs:*:{self.account}:log-group:/aws/lambda/*"
                ]
            )
        )

        canary_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:ListAllMyBuckets",
                    "xray:PutTraceSegments",
                    "cloudwatch:PutMetricData",
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface"
                ],
                resources=["*"]
            )
        )

        return canary_role

    def canary_script_data(self, config):
        canary_script = '''import json
import os
import http.client
from selenium.webdriver.common.by import By
import urllib.parse
from aws_synthetics.selenium import synthetics_webdriver as syn_webdriver
from aws_synthetics.common import synthetics_logger as logger

def verify_request(method, url, post_data=None, headers={}):
    parsed_url = urllib.parse.urlparse(url)
    user_agent = str(syn_webdriver.get_canary_user_agent_string())
    if "User-Agent" in headers:
        headers["User-Agent"] = f"{user_agent} {headers['User-Agent']}"
    else:
        headers["User-Agent"] = user_agent

    logger.info(f"Making request with Method: '{method}' URL: {url}: Data: {json.dumps(post_data)} Headers: {json.dumps(headers)}")

    if parsed_url.scheme == "https":
        conn = http.client.HTTPSConnection(parsed_url.hostname, parsed_url.port)
    else:
        conn = http.client.HTTPConnection(parsed_url.hostname, parsed_url.port)

    conn.request(method, url, post_data, headers)
    response = conn.getresponse()
    logger.info(f"Status Code: {response.status}")
    logger.info(f"Response Headers: {json.dumps(response.headers.as_string())}")

    if not response.status or response.status < 200 or response.status > 299:
        try:
            logger.error(f"Response: {response.read().decode()}")
        finally:
            if response.reason:
                conn.close()
                raise Exception(f"Failed: {response.reason}")
            else:
                conn.close()
                raise Exception(f"Failed with status code: {response.status}")

    logger.info(f"Response: {response.read().decode()}")
    logger.info("HTTP request successfully executed.")
    conn.close()
	
def handler(event, context):
    
    url = os.environ['TARGET_URL']  # Read the environment variable for the URL
    method = 'GET'
    postData = ""
    headers1 = {}
    
    verify_request(method, url, None, headers1)
    logger.info("Canary successfully executed.")
'''

        return canary_script

    def create_canary(self, config, vpc, subnet_ids, security_group_id, canary_role,
                      target_url, canary_script, app_name, bucket_name):
        # Create the Canary inside the existing VPC
        canary = synthetics.CfnCanary(self,
                                      id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-{app_name}-{config['resource_suffix']}",
                                      name=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-{app_name}-{config['resource_suffix']}",
                                      runtime_version="syn-python-selenium-5.0",
                                      artifact_s3_location=f"s3://{bucket_name}/canary/{self.region}/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-canary-{app_name}-{config['resource_suffix']}",
                                      execution_role_arn=canary_role.role_arn,
                                      code=synthetics.CfnCanary.CodeProperty(
                                          handler="index.handler",
                                          script=canary_script
                                      ),
                                      run_config=synthetics.CfnCanary.RunConfigProperty(
                                          active_tracing=False,
                                          environment_variables={
                                              'TARGET_URL': target_url  # Set the environment variable here
                                          }
                                      ),
                                      schedule=synthetics.CfnCanary.ScheduleProperty(
                                          expression="rate(1 minute)"  # Set the canary to run every 1 minute
                                      ),
                                      vpc_config=synthetics.CfnCanary.VPCConfigProperty(
                                          vpc_id=vpc.vpc_id,
                                          subnet_ids= subnet_ids,
                                          security_group_ids=security_group_id
                                      ),
                                      success_retention_period=30,
                                      failure_retention_period=30,
                                      start_canary_after_creation=True
                                      )

        return canary

    # Create task execution role for FIS service
    def create_fis_role(self, config) -> iam.Role:
        exec_role = iam.Role(
            self,
            id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-exec-role-{config['resource_suffix']}",
            assumed_by=iam.ServicePrincipal('fis.amazonaws.com'),
            role_name=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-exec-role-{config['resource_suffix']}"
        )

        exec_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions= ["fis:*"],
                resources=[
                    f"arn:aws:fis:*:{config['workload_account']}:experiment-template/*",
                    f"arn:aws:fis:*:{config['workload_account']}:safety-lever/*",
                    f"arn:aws:fis:*:{config['workload_account']}:action/*",
                    f"arn:aws:fis:*:{config['workload_account']}:experiment/*"
                ]
            )
        )

        exec_role.add_to_policy(
            iam.PolicyStatement(effect=iam.Effect.ALLOW,
                                actions=[
                                    "fis:ListExperimentTemplates",
                                    "fis:ListActions",
                                    "fis:ListTargetResourceTypes",
                                    "fis:ListExperiments",
                                    "fis:GetTargetResourceType",
                                    "logs:CreateLogDelivery",
                                    "logs:UpdateLogDelivery",
                                    "logs:GetLogDelivery",
                                    "logs:ListLogDeliveries",
                                    "ec2:CreateNetworkInterface",
                                    "ec2:DeleteNetworkInterfacePermission",
                                    "ec2:DescribeNetworkInterfaces",
                                    "ec2:CreateNetworkInterfacePermission",
                                    "ec2:DescribeVpcs",
                                    "ec2:CreateTags",
                                    "ec2:DeleteNetworkInterface",
                                    "ec2:DescribeSubnets"
                                ],
                                resources=["*"]
                                )
        )

        exec_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:CreateServiceLinkedRole"],
                resources=[f"arn:aws:iam::{config['workload_account']}:role/*"]
            )
        )

        exec_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=["arn:aws:logs:*:*:log-group:/sw/fis/*:*"]
            )
        )

        exec_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["elasticache:InterruptClusterAzPower","elasticache:TestFailover","elasticache:FailoverGlobalReplicationGroup"],
                resources=[f"arn:aws:elasticache:*:{self.account}:replicationgroup:*",f"arn:aws:elasticache::{self.account}:globalreplicationgroup:*"]
            )
        )

        exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSFaultInjectionSimulatorNetworkAccess")
        )

        exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSFaultInjectionSimulatorRDSAccess")
        )

        exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSFaultInjectionSimulatorECSAccess")
        )

        exec_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSFaultInjectionSimulatorSSMAccess")
        )

        return exec_role

    def lookup_vpc(self, config):
        # vpc lookup from account
        vpc = ec2.Vpc.from_lookup(
            self,
            f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-vpc-{config['resource_suffix']}",
            vpc_id=f"{config['vpc_id']}"
        )

        return vpc

    def lookup_subnet(self, subnet_1, subnet_2):
        subnet = ec2.SubnetSelection(
            one_per_az=True,
            subnet_filters=[
                ec2.SubnetFilter.by_ids([
                    f"{subnet_1}", f"{subnet_2}"
                ])
            ]
        )

        return subnet

    def create_log_group(self, config):
        log_group = logs.LogGroup(
            self,
            id=f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-fis-logs-{config['resource_suffix']}",
            log_group_name=f"/sw/fis/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-fis-logs-{config['resource_suffix']}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        return log_group

    def create_fis_network_subnet_experiment(self, config, subnet_list, test_name, role, log_group, az_name, db_app_list):
        db_arns = []
        for db_app_name in db_app_list.split(","):
            db_arns.append(f"arn:aws:rds:{self.region}:{self.account}:cluster:{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{db_app_name}-{config['resource_suffix']}")

        subnet_arns = []
        for subnet in subnet_list.split(","):
            subnet_arns.append(f"arn:aws:ec2:{self.region}:{self.account}:subnet/{subnet}")

        experiment_template = fis.CfnExperimentTemplate(self,
                                                        f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-azfailure-experiment-{test_name}-{config['resource_suffix']}",
                                                        description=f"AZ Power Failure Simulation in {test_name}",
                                                        role_arn=role.role_arn,
                                                        targets={
                                                            "SubnetDown": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:ec2:subnet",
                                                                resource_arns=subnet_arns,
                                                                selection_mode="ALL",
                                                                parameters={}
                                                            ),
                                                            "RDSFailover": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:rds:cluster",
                                                                resource_tags={"sw:product" : "mra"},
                                                                selection_mode="ALL",
                                                                parameters={
                                                                    "writerAvailabilityZoneIdentifiers": az_name
                                                                }
                                                            ),
                                                            "ElastiCacheCluster": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:elasticache:replicationgroup",
                                                                resource_tags={"sw:product" : "mra"},
                                                                selection_mode="ALL",
                                                                parameters={
                                                                    "availabilityZoneIdentifier": az_name
                                                                }
                                                            )
                                                        },
                                                        actions={
                                                            "DisruptNetworkConnectivity": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:network:disrupt-connectivity",
                                                                description=f"Disrupt network connectivity for subnets in {test_name}",
                                                                parameters={
                                                                    "duration": "PT15M",  # Duration of the network disruption
                                                                    "scope": "all"
                                                                },
                                                                targets={
                                                                    "Subnets": "SubnetDown"
                                                                }
                                                            ),
                                                            "RDSFailoverAction": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:rds:failover-db-cluster",
                                                                description="Aurora Serverless RDS Failover DB",
                                                                parameters={},
                                                                targets={
                                                                    "Clusters": "RDSFailover"
                                                                }
                                                            ),
                                                            "PauseElastiCache": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:elasticache:replicationgroup-interrupt-az-power",
                                                                parameters={
                                                                    "duration": "PT15M"
                                                                },
                                                                targets={
                                                                    "ReplicationGroups": "ElastiCacheCluster"
                                                                }
                                                            ),
                                                            "FISWait": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:fis:wait",
                                                                parameters={
                                                                    "duration": "PT15M"
                                                                },
                                                                targets={}
                                                            )
                                                        },
                                                        experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
                                                            account_targeting="single-account",
                                                            empty_target_resolution_mode="fail"
                                                        ),
                                                        log_configuration=fis.CfnExperimentTemplate.ExperimentTemplateLogConfigurationProperty(
                                                            cloud_watch_logs_configuration={
                                                                "LogGroupArn": log_group.log_group_arn
                                                            },
                                                            log_schema_version=2
                                                        ),
                                                        stop_conditions=[
                                                            {
                                                                "source": "none"
                                                            }
                                                        ],
                                                        tags={
                                                            "Name": f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-azfailure-experiment-{test_name}-{config['resource_suffix']}",
                                                            "Environment": f"{config['app_env']}"
                                                        }
                                                        )

        return experiment_template

    def create_fis_ecs_cluster_drain_experiment(self, config, role, log_group, percent):
        experiment_template = fis.CfnExperimentTemplate(self,
                                                        f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-drain-p{percent}-experiment-{config['resource_suffix']}",
                                                        description=f"Drain ECS cluster container instances in percent {percent}",
                                                        role_arn=role.role_arn,
                                                        targets={
                                                            "ECSClusterDrain": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:ecs:cluster",
                                                                resource_arns=[
                                                                    f"arn:aws:ecs:{self.region}:{self.account}:cluster/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-ecs-cluster-infra-{config['resource_suffix']}"
                                                                ],
                                                                selection_mode="ALL"
                                                            )
                                                        },
                                                        actions={
                                                            "ECSDrain": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:ecs:drain-container-instances",
                                                                description=f"Drain ECS cluster container instances in percent {percent}",
                                                                parameters={
                                                                    "drainagePercentage": percent,  # Duration of the network disruption
                                                                    "duration": "PT15M"
                                                                },
                                                                targets={
                                                                    "Clusters": "ECSClusterDrain"
                                                                }
                                                            )
                                                        },
                                                        experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
                                                            account_targeting="single-account",
                                                            empty_target_resolution_mode="fail"
                                                        ),
                                                        log_configuration=fis.CfnExperimentTemplate.ExperimentTemplateLogConfigurationProperty(
                                                            cloud_watch_logs_configuration={
                                                                "LogGroupArn": log_group.log_group_arn
                                                            },
                                                            log_schema_version=2
                                                        ),
                                                        stop_conditions=[
                                                            {
                                                                "source": "none"
                                                            }
                                                        ],
                                                        tags={
                                                            "Name": f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-drain-p{percent}-experiment-{config['resource_suffix']}",
                                                            "Environment": f"{config['app_env']}"
                                                        }
                                                        )

        return experiment_template

    def create_fis_rds_failover_experiment(self, config, role, log_group, db_app_name):
        experiment_template = fis.CfnExperimentTemplate(self,
                                                        f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-rdsfailover-{db_app_name}-experiment-{config['resource_suffix']}",
                                                        description=f"Aurora Serverless RDS Failover DB {db_app_name}",
                                                        role_arn=role.role_arn,
                                                        targets={
                                                            "RDSFailover": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:rds:cluster",
                                                                resource_arns=[
                                                                    f"arn:aws:rds:{self.region}:{self.account}:cluster:{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{db_app_name}-{config['resource_suffix']}"
                                                                ],
                                                                selection_mode="ALL"
                                                            )
                                                        },
                                                        actions={
                                                            "RDSFailoverAction": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:rds:failover-db-cluster",
                                                                description=f"Aurora Serverless RDS Failover DB {db_app_name}",
                                                                parameters={},
                                                                targets={
                                                                    "Clusters": "RDSFailover"
                                                                }
                                                            )
                                                        },
                                                        experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
                                                            account_targeting="single-account",
                                                            empty_target_resolution_mode="fail"
                                                        ),
                                                        log_configuration=fis.CfnExperimentTemplate.ExperimentTemplateLogConfigurationProperty(
                                                            cloud_watch_logs_configuration={
                                                                "LogGroupArn": log_group.log_group_arn
                                                            },
                                                            log_schema_version=2
                                                        ),
                                                        stop_conditions=[
                                                            {
                                                                "source": "none"
                                                            }
                                                        ],
                                                        tags={
                                                            "Name": f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-rdsfailover-{db_app_name}-experiment-{config['resource_suffix']}",
                                                            "Environment": f"{config['app_env']}"
                                                        }
                                                        )

        return experiment_template

    def create_fis_ecs_task_stop_experiment(self, config, role, log_group, ecs_app_name, percent):
        experiment_template = fis.CfnExperimentTemplate(self,
                                                        f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-taskstop-{ecs_app_name}-p{percent}-experiment-{config['resource_suffix']}",
                                                        description=f"Stop ECS Task for service app {ecs_app_name} with percent {percent}",
                                                        role_arn=role.role_arn,
                                                        targets={
                                                            "ECSTaskStop": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:ecs:task",
                                                                parameters={
                                                                    "cluster": f"{config['resource_prefix']}-mra-{config['app_env']}-ecs-cluster-fra-{config['resource_suffix']}",
                                                                    "service": f"{config['resource_prefix']}-mra-{config['app_env']}-ecs-service-fra-{config['resource_suffix']}"
                                                                },
                                                                selection_mode=f"PERCENT({percent})"
                                                            )
                                                        },
                                                        actions={
                                                            "ECSTaskStopAction": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:ecs:stop-task",
                                                                description=f"ECS Task Stop for service app {ecs_app_name} with percent {percent}",
                                                                parameters={},
                                                                targets={
                                                                    "Tasks": "ECSTaskStop"
                                                                }
                                                            )
                                                        },
                                                        experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
                                                            account_targeting="single-account",
                                                            empty_target_resolution_mode="fail"
                                                        ),
                                                        log_configuration=fis.CfnExperimentTemplate.ExperimentTemplateLogConfigurationProperty(
                                                            cloud_watch_logs_configuration={
                                                                "LogGroupArn": log_group.log_group_arn
                                                            },
                                                            log_schema_version=2
                                                        ),
                                                        stop_conditions=[
                                                            {
                                                                "source": "none"
                                                            }
                                                        ],
                                                        tags={
                                                            "Name": f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-taskstop-{ecs_app_name}-p{percent}-experiment-{config['resource_suffix']}",
                                                            "Environment": f"{config['app_env']}"
                                                        }
                                                        )

        return experiment_template

    def create_fis_ecs_task_cpustress_experiment(self, config, role, log_group, ecs_app_name, percent):
        experiment_template = fis.CfnExperimentTemplate(self,
                                                        f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-taskcpustress-{ecs_app_name}-p{percent}-experiment-{config['resource_suffix']}",
                                                        description=f"CPU Stress ECS Task for service app {ecs_app_name} with percent {percent}",
                                                        role_arn=role.role_arn,
                                                        targets={
                                                            "ECSTaskCPUStress": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:ecs:task",
                                                                parameters={
                                                                    "cluster": f"{config['resource_prefix']}-mra-{config['app_env']}-ecs-cluster-fra-{config['resource_suffix']}",
                                                                    "service": f"{config['resource_prefix']}-mra-{config['app_env']}-ecs-service-fra-{config['resource_suffix']}"
                                                                },
                                                                selection_mode=f"PERCENT({percent})"
                                                            )
                                                        },
                                                        actions={
                                                            "ECSTaskCPUStressAction": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:ecs:task-cpu-stress",
                                                                description=f"ECS Task CPU Stress for service app {ecs_app_name} with percent {percent}",
                                                                parameters={
                                                                    "duration": "PT15M",
                                                                    "installDependencies": "true",
                                                                    "percent": "100",
                                                                    "workers": "0"
                                                                },
                                                                targets={
                                                                    "Tasks": "ECSTaskCPUStress"
                                                                }
                                                            )
                                                        },
                                                        experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
                                                            account_targeting="single-account",
                                                            empty_target_resolution_mode="fail"
                                                        ),
                                                        log_configuration=fis.CfnExperimentTemplate.ExperimentTemplateLogConfigurationProperty(
                                                            cloud_watch_logs_configuration={
                                                                "LogGroupArn": log_group.log_group_arn
                                                            },
                                                            log_schema_version=2
                                                        ),
                                                        stop_conditions=[
                                                            {
                                                                "source": "none"
                                                            }
                                                        ],
                                                        tags={
                                                            "Name": f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-taskcpustress-{ecs_app_name}-p{percent}-experiment-{config['resource_suffix']}",
                                                            "Environment": f"{config['app_env']}"
                                                        }
                                                        )

        return experiment_template

    def create_fis_ecs_task_iostress_experiment(self, config, role, log_group, ecs_app_name, percent):
        experiment_template = fis.CfnExperimentTemplate(self,
                                                        f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-taskiostress-{ecs_app_name}-p{percent}-experiment-{config['resource_suffix']}",
                                                        description=f"IO Stress ECS Task for service app {ecs_app_name} with percent {percent}",
                                                        role_arn=role.role_arn,
                                                        targets={
                                                            "ECSTaskIOStress": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                                                                resource_type="aws:ecs:task",
                                                                parameters={
                                                                    "cluster": f"{config['resource_prefix']}-mra-{config['app_env']}-ecs-cluster-fra-{config['resource_suffix']}",
                                                                    "service": f"{config['resource_prefix']}-mra-{config['app_env']}-ecs-service-fra-{config['resource_suffix']}"
                                                                },
                                                                selection_mode=f"PERCENT({percent})"
                                                            )
                                                        },
                                                        actions={
                                                            "ECSTaskIOStressAction": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                                                                action_id="aws:ecs:task-io-stress",
                                                                description=f"ECS Task IO Stress for service app {ecs_app_name} with percent {percent}",
                                                                parameters={
                                                                    "duration": "PT15M",
                                                                    "installDependencies": "true",
                                                                    "percent": "80",
                                                                    "workers": "1"
                                                                },
                                                                targets={
                                                                    "Tasks": "ECSTaskIOStress"
                                                                }
                                                            )
                                                        },
                                                        experiment_options=fis.CfnExperimentTemplate.ExperimentTemplateExperimentOptionsProperty(
                                                            account_targeting="single-account",
                                                            empty_target_resolution_mode="fail"
                                                        ),
                                                        log_configuration=fis.CfnExperimentTemplate.ExperimentTemplateLogConfigurationProperty(
                                                            cloud_watch_logs_configuration={
                                                                "LogGroupArn": log_group.log_group_arn
                                                            },
                                                            log_schema_version=2
                                                        ),
                                                        stop_conditions=[
                                                            {
                                                                "source": "none"
                                                            }
                                                        ],
                                                        tags={
                                                            "Name": f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-ecs-taskiostress-{ecs_app_name}-p{percent}-experiment-{config['resource_suffix']}",
                                                            "Environment": f"{config['app_env']}"
                                                        }
                                                        )

        return experiment_template

    def __init__(self, scope: Construct, construct_id: str, resource_config, app_config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Get configuration variables from resource file
        config = resource_config
        app_config = app_config

        bucket = self.create_artifact_store(config)

        # vpc lookup from account
        vpc = self.lookup_vpc(config=config)

        # Always create log group (regional resource)
        log_group = self.create_log_group(config=config)

        # Only create IAM roles and other global resources in the primary region
        if config.get("deployment_region") == config.get("primary_region"):
            fis_role = self.create_fis_role(config=config)

            # FIS Experiment Template - App AZ subnet fail
            self.create_fis_network_subnet_experiment(config=config, subnet_list=config['subnet_az1_list'],
                                                      test_name="az1", role=fis_role, log_group=log_group,
                                                      az_name=config['az1_name'], db_app_list=config['db_clusters']
                                                      )
            self.create_fis_network_subnet_experiment(config=config, subnet_list=config['subnet_az2_list'],
                                                      test_name="az2", role=fis_role, log_group=log_group,
                                                      az_name=config['az2_name'], db_app_list=config['db_clusters']
                                                      )

            # FIS Experiment Template - ECS stop
            for ecs_name in config['ecs_services'].split(","):
                for percent_val in config['ecs_taks_percents'].split(","):
                    self.create_fis_ecs_task_stop_experiment(config=config, role=fis_role, log_group=log_group,
                                                             ecs_app_name=ecs_name, percent=percent_val
                                                             )

            # DO NOT ENABLE ---- FIS Experiment Template - ECS CPU stress
            # for ecs_name in config['ecs_services'].split(","):
            #     for percent_val in config['ecs_taks_percents'].split(","):
            #         self.create_fis_ecs_task_cpustress_experiment(config=config, role=fis_role, log_group=log_group,
            #             ecs_app_name=ecs_name, percent=percent_val
            #         )

            # DO NOT ENABLE ---- FIS Experiment Template - ECS IO stress - Future test cases
            # for ecs_name in config['ecs_services'].split(","):
            #     for percent_val in config['ecs_taks_percents'].split(","):
            #         self.create_fis_ecs_task_iostress_experiment(config=config, role=fis_role, log_group=log_group,
            #             ecs_app_name=ecs_name, percent=percent_val
            #         )

            # for percent_val in config['ecs_drain_percents'].split(","):
            #     self.create_fis_ecs_cluster_drain_experiment(config=config, role=fis_role,
            #         log_group=log_group, percent=percent_val
            #     )

            for db_name in config['db_clusters'].split(","):
                self.create_fis_rds_failover_experiment(config=config, role=fis_role, log_group=log_group, db_app_name=db_name)

        # synthetics canary
        if config.get("deployment_region") == config.get("primary_region"):
            canary_role = self.create_canary_role(config=config, bucket_name=bucket.bucket_name)
        canary_script = self.canary_script_data(config=config)
        canary_sg = self.create_canary_security_group(config=config, vpc=vpc)

        for app in app_config:
            for app_name, app_url in zip(app.get_app_names(), app.get_app_urls()):
                self.create_canary(config=config, vpc=vpc, subnet_ids=[app.get_subnet_id()],
                                   security_group_id=[canary_sg.security_group_id], canary_role=canary_role,
                                   target_url=app_url, canary_script=canary_script,
                                   app_name=f"{app_name}-{app.get_canary_name()}", bucket_name=bucket.bucket_name)
