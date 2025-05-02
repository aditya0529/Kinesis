#!/usr/bin/env python3

import configparser
import aws_cdk as cdk
import os
import json
from aws_cdk import (
    Aspects,
    Tags,
)
from cdk_nag import AwsSolutionsChecks, NagSuppressions
from stack.cloud_infra import cloud_infra
from util.app_config import ApplicationConfig

def load_applications_from_json(file_path):
    with open(file_path, 'r') as f:
        data = json.load(f)
    return [ApplicationConfig(app) for app in data]

def get_def_stack_synth(config):
    return cdk.DefaultStackSynthesizer(
        cloud_formation_execution_role=f"arn:aws:iam::{config['workload_account']}:role/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}dply-role-main-a",
        deploy_role_arn=f"arn:aws:iam::{config['workload_account']}:role/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}dply-role-main-a",
        file_asset_publishing_role_arn=f"arn:aws:iam::{config['workload_account']}:role/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}dply-role-main-a",
        image_asset_publishing_role_arn=f"arn:aws:iam::{config['workload_account']}:role/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}dply-role-main-a",
        lookup_role_arn=f"arn:aws:iam::{config['workload_account']}:role/{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}dply-role-main-a",
        file_assets_bucket_name=f"{config['asset_prefix']}-{config['workload_account']}-{config['deployment_region']}-{config['resource_suffix']}",
        # image_assets_repository_name=cdk_custom_configs.get('bootstrap_image_assets_repository_name')
        bootstrap_stack_version_ssm_parameter=f"{config['bootstrap_stack_version']}"
    )

if __name__ == "__main__":
    # Reading Application infra resource varibales using git branch name
    branch_name = os.getenv("SRC_BRANCH", "dev")

    # Multi-region config file mapping
    region_config_files = {
        "eu-central-1": "resource.eu-central-1.config",
        "us-east-1": "resource.us-east-1.config",
    }
    regions = list(region_config_files.keys())
    primary_region = regions[0]  # Use the first region as primary for IAM
    stacks = []

    # Initializing CDK app
    app = cdk.App()

    for region in regions:
        config_parser = configparser.ConfigParser()
        config_parser.read(region_config_files[region])
        config = config_parser[branch_name]
        config = dict(config)
        config["primary_region"] = primary_region
        config["deployment_region"] = region
        app_config = load_applications_from_json(f"config/canary_app_list_{branch_name}.json")
        stack = cloud_infra(
            app,
            f"{config['resource_prefix']}-{config['service_name']}-{config['app_env']}-{config['app_name']}-infra-stack-{region}-{config['resource_suffix']}",
            resource_config=config,
            app_config=app_config,
            env=cdk.Environment(account=f"{config['workload_account']}", region=region),
            synthesizer=get_def_stack_synth(config)
        )
        Tags.of(stack).add("sw:application", "mra")
        Tags.of(stack).add("sw:product", "mra")
        Tags.of(stack).add("sw:environment", f"{config['app_env']}")
        Tags.of(stack).add("sw:cost_center", f"{config['cost_center']}")
        Aspects.of(app).add(AwsSolutionsChecks())
        NagSuppressions.add_stack_suppressions(stack, [
            {'id': 'AwsSolutions-S1', 'reason': 'Cloudtrail already capturing access of S3 data plane'},
            {'id': 'AwsSolutions-IAM5', 'reason': 'IAM policy with resource star'},
            {'id': 'AwsSolutions-IAM4', 'reason': 'IAM managed policy'}
        ])
        stacks.append(stack)

    # Synthesize and produce CloudFormation template
    app.synth()
