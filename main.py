import boto3
import time
from kubernetes import client, config
import os
import json
from datetime import datetime, timezone, timedelta

# Environment variables
ENVIRONMENT = os.getenv("ENVIRONMENT", "staging")
NAMESPACE = os.getenv("NAMESPACE", "default")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "default-queue-url")
REGION_NAME = os.getenv("REGION_NAME", "us-east-1")
DOCKER_IMAGE = os.getenv("DOCKER_IMAGE", "ghcr.io/openhistoricalmap/tiler-server:0.0.1-0.dev.git.1735.h825f665")
NODEGROUP_TYPE = os.getenv("NODEGROUP_TYPE", "job_large")
MAX_ACTIVE_JOBS = int(os.getenv("MAX_ACTIVE_JOBS", 2))
DELETE_OLD_JOBS_AGE = int(os.getenv("DELETE_OLD_JOBS_AGE", 86400))

MIN_ZOOM=os.getenv("MIN_ZOOM", 8)
MAX_ZOOM=os.getenv("MAX_ZOOM", 16)

# Initialize AWS SQS client
sqs = boto3.client("sqs", region_name=REGION_NAME)

# Load Kubernetes configuration
config.load_incluster_config()
batch_v1 = client.BatchV1Api()
core_v1 = client.CoreV1Api()

def get_active_jobs_count():
    """
    Returns the number of jobs in the namespace with names starting with 'tiler-purge-seed-'
    where the associated pods are in 'Running' or 'Pending' state.
    """
    print ("============Checking number of active or pending pods=============")
    jobs = batch_v1.list_namespaced_job(namespace=NAMESPACE)
    active_jobs_count = 0

    for job in jobs.items:
        # Check if the job name matches the desired prefix
        if not job.metadata.name.startswith("tiler-purge-seed-"):
            continue

        # List pods associated with the job
        label_selector = f"job-name={job.metadata.name}"
        pods = core_v1.list_namespaced_pod(namespace=NAMESPACE, label_selector=label_selector)

        # Check if any pod is in Running or Pending state
        for pod in pods.items:
            print("------")
            print(pod.status.phase)
            if pod.status.phase in ["Running", "Pending"]:
                active_jobs_count += 1
                break  # Count the job only once, even if it has multiple pods in these states

    return active_jobs_count



def delete_old_pods():
    """List and delete Kubernetes jobs older than the specified age."""
    jobs = batch_v1.list_namespaced_job(namespace=NAMESPACE)
    now = datetime.now(timezone.utc)
    deleted_count = 0

    for job in jobs.items:
        # Skip jobs that don't start with the prefix (e.g., "tiler-purge-seed-")
        if not job.metadata.name.startswith("tiler-purge-seed-"):
            continue

        # Check if the job has a 'Complete' condition
        conditions = job.status.conditions or []
        for condition in conditions:
            if condition.type == "Complete" and condition.status == "True":
                # Calculate the age of the job
                completion_time = condition.last_transition_time
                if completion_time and now - completion_time > timedelta(seconds=DELETE_OLD_JOBS_AGE):
                    # Delete the job
                    try:
                        batch_v1.delete_namespaced_job(
                            name=job.metadata.name,
                            namespace=NAMESPACE,
                            body=client.V1DeleteOptions(propagation_policy="Background"),
                        )
                        print(f"Deleted job older than {DELETE_OLD_JOBS_AGE} seconds: {job.metadata.name}")
                        deleted_count += 1
                    except Exception as e:
                        print(f"Error deleting job {job.metadata.name}: {e}")

    print(f"Deleted {deleted_count} jobs older than {DELETE_OLD_JOBS_AGE} seconds.")


def create_kubernetes_job(file_url, file_name):
    """Create a Kubernetes Job to process a file."""
    config_map_name = f"{ENVIRONMENT}-tiler-server-cm"
    job_name = f"tiler-purge-seed-{file_name}"
    job_manifest = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name},
        "spec": {
            "template": {
                "spec": {
                    "nodeSelector": {
                        "nodegroup_type": NODEGROUP_TYPE
                    },
                    "containers": [
                        {
                            "name": "tiler-purge-seed",
                            "image": DOCKER_IMAGE,
                            "command": ["sh", "./purge_and_seed.sh"],
                            "envFrom": [
                                {"configMapRef": {"name": config_map_name}},
                            ],
                            "env": [
                                {
                                    "name": "IMPOSM_EXPIRED_FILE",
                                    "value": file_url,
                                },
                                {
                                    "name": "MIN_ZOOM",
                                    "value": str(MIN_ZOOM),
                                },
                                {
                                    "name": "MAX_ZOOM",
                                    "value": str(MAX_ZOOM),
                                }
                            ],
                        }
                    ],
                    "restartPolicy": "Never",
                }
            },
            "backoffLimit": 1,
        },
    }

    # Create the Kubernetes Job
    batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job_manifest)
    print(f"Kubernetes Job '{job_name}' created for file: {file_url}")


def process_sqs_messages():
    """Process messages from the SQS queue and create Kubernetes Jobs for each file."""
    while True:
        # Delete pods older than the specified age
        delete_old_pods()

        # Wait until the number of active jobs is below the limit
        while get_active_jobs_count() >= MAX_ACTIVE_JOBS:
            print(f"Waiting for active jobs to drop below {MAX_ACTIVE_JOBS}...")
            time.sleep(60)

        response = sqs.receive_message(
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=10,  # Maximum number of messages per request
            WaitTimeSeconds=10,  # Long polling to reduce costs
        )

        messages = response.get("Messages", [])
        if not messages:
            print("No messages in the queue.")
            time.sleep(5)
            continue

        for message in messages:
            try:
                # Parse the message body
                body = json.loads(message["Body"])

                # Check for S3 event messages
                if "Records" in body and body["Records"][0]["eventSource"] == "aws:s3":
                    record = body["Records"][0]
                    bucket_name = record["s3"]["bucket"]["name"]
                    object_key = record["s3"]["object"]["key"]

                    # Construct the S3 file URL
                    file_url = f"s3://{bucket_name}/{object_key}"

                    # Extract the file name from the object key
                    file_name = os.path.basename(object_key)
                    print(f"Processing message for file: {file_url}, extracted file name: {file_name}")

                    # Create a Kubernetes Job for this file
                    create_kubernetes_job(file_url, file_name)

                # Check for TestEvent messages and ignore them
                elif "Event" in body and body["Event"] == "s3:TestEvent":
                    print("Test event detected. Ignoring...")

                # Delete the message from the SQS queue after processing
                sqs.delete_message(
                    QueueUrl=SQS_QUEUE_URL,
                    ReceiptHandle=message["ReceiptHandle"],
                )
                print("Message processed and deleted.")

            except Exception as e:
                print(f"Error processing message: {e}")
                continue

        # Sleep 60 seconds
        time.sleep(10)

if __name__ == "__main__":
    print("Starting SQS message processing...")
    process_sqs_messages()
    