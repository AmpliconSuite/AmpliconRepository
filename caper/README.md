# AmpliconRepository - Caper App

## Local Development Setup

### AWS CLI (Static File Sync)
For local development, the server syncs static files to S3 in the background. To enable this, you must have the AWS CLI installed:

1. **Install AWS CLI**:
   ```bash
   curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
   unzip awscliv2.zip
   sudo ./aws/install
   ```

2. **Configure Profile**:
   To sync files to the project's S3 bucket, you need to configure the `amprepo` profile with credentials. Obtain the Access Key ID and Secret Access Key from the project developers.
   ```bash
   aws configure --profile amprepo
   ```
   (When prompted, enter the keys, set the region to `us-east-1` (or the appropriate region), and leave the output format as `json`).
