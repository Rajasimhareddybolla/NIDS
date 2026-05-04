# AWS Deployment Guide for NIDS Streamlit Application

This guide demonstrates how to deploy the NIDS Analysis application (built with Streamlit and MongoDB) to an AWS EC2 instance using Docker Compose.

## 1. Prerequisites

- An active **AWS Account**. You can log in to the AWS Management Console here: [https://aws.amazon.com/console/](https://aws.amazon.com/console/).
- Basic understanding of **EC2** and **Security Groups**.
- **Docker** and **Docker Compose** installed on your local machine to test before deploying.

## 2. Launch an AWS EC2 Instance

1. Navigate to the **EC2 Dashboard** in the AWS Console.
2. Click **Launch Instance**.
3. Choose an **Amazon Linux 2023** or **Ubuntu Server** AMI.
4. Select an instance type (e.g., `t2.micro` or `t3.small` depending on the memory needed).
5. Create or select an existing **Key Pair** (`.pem` file) for SSH access.
6. In **Network Settings**, create a new Security Group or update an existing one:
   - **Allow SSH** (port 22) from your IP.
   - **Allow Custom TCP** (port 8501) from Anywhere (0.0.0.0/0). This is for the Streamlit App.

## 3. SSH Login & Install Docker on EC2

To log in to the EC2 instance you just created, you'll use SSH with the key pair you downloaded (`.pem` file).

SSH into your freshly launched instance from your local terminal:

```bash
# Make sure your key has the correct permissions
chmod 400 "your-key-pair.pem"

# SSH Login to AWS EC2
ssh -i "your-key-pair.pem" ec2-user@<your-ec2-public-ip>
```

Install Docker and Docker Compose:

```bash
# For Amazon Linux:
sudo yum update -y
sudo yum install docker git -y
sudo service docker start
sudo usermod -a -G docker ec2-user

# Get Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```
*Note: Logout and log back in, or run `newgrp docker` to apply docker permissions.*

## 4. Clone and Build the Application

Clone your repository (or copy the files securely to the EC2 via SCP):

```bash
git clone <your-repository-url>
cd <your-repo-folder>
```

Build and spin up the environment containing Streamlit and MongoDB:

```bash
# This uses the docker-compose.yml file
docker-compose up -d --build
```

You can verify the containers are up by running:
```bash
docker ps
```

## 5. Seed the MongoDB Database

The MongoDB needs data to show on the dashboard. Inside the directory where your JSON log data is, run the script to populate the Database.

```bash
# Execute within a python environment or inside docker
pip install pymongo
MONGO_URI="mongodb://localhost:27017/" python scripts/seed_mongo.py
```

*Since MongoDB is exposed internally by Docker to the host via port `27017` in the `docker-compose.yml`, you can safely run the seeding script from the EC2 host.*

## 6. Access the Application

Once everything is up and seeded, open your web browser and go to:

`http://<your-ec2-public-ip>:8501`

You will see the **NIDS Evaluation Dashboard 🛡️** populated with the model metrics and Kafka stream diagnostics loaded from MongoDB!
