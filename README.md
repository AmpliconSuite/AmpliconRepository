# [AmpliconRepository.org](https://ampliconrepository.org/)

#### Authors: Forrest Kim, Jens Luebeck, Ted Liefeld, Edwin Huang, Gino Prasad, Rohil Ahuja, Rishaan Kenkre, Tushar Agashe, Devika Torvi, Madalina Giurgiu, Thorin Tabor, Vineet Bafna

---

This is the main repository for the AmpliconRepository website. The documentation below provides intsructions on deploying the site locally, for development purposes.

- [How to install the development environment for AmpliconRepository](#aa-env-install)
- [How to set up your development environment using docker compose](#dev-docker)
- [Testing datasets](#test-datasets) 
- [Pushing changes to GitHub and merging PRs](#pr)
- [Using the development server](#dev-server)
- [Logging in as admin](#admin-logging)
- [How to deploy and update the production server for AmpliconRepository](#deploy)


## There are two options for running the server locally: 
**[Option A](#aa-env-install)**: Manually install modules and configure the environment step-by-step.

**[Option B](#dev-docker)**: Use Docker to deploy the server and its environment on your system.

## Option A - install the development environment for AmpliconRepository: <a name="aa-env-install"></a> 

## Requirements
- Python Virtual Environment (3.8 or higher)

## 1. Clone the repository from GitHub
- Clone repo using https, ssh, or GitHub Desktop to your local machine

## 2. Set up the virtual environment and install packages:
- In a terminal window, move to the cloned GitHub repo
- Go to the AmpliconRepository top level directory (should see `requirements.txt`)
#### Option A: Using python's environment manager
- Create a new Python virtual environment:
> `python -m venv ampliconenv`
- Activate the new environment (you need to do this everytime before running the server):
> `source ampliconenv/bin/activate`
- Install required packages
> `pip install -r requirements.txt`
#### Option B: Using conda's environment manager
- Create a new Conda environment
> `conda create -n "ampliconenv" python>=3.8.0`
- To activate
> `conda activate ampliconenv`
- Install pip to that environment 
> `conda install pip -n ampliconenv`
- Install required packages
> `~/[anaconda3/miniconda3]/envs/ampliconenv/bin/pip install -r requirements.txt`



## 3. Set up MongoDB locally (for development)
- Install MongoDB
  - In Ubuntu this can be done with `sudo apt install mongodb-server-core`
    - For newer versions of Ubuntu (e.g. 22.04+), follow the instructions here: https://www.fosstechnix.com/how-to-install-mongodb-on-ubuntu-22-04-lts/
  - In macOS this can be done with
    >`git config --global init.defaultBranch main`
    
    >`brew tap mongodb/brew`
    
    >`brew install mongodb-community@6.0`
  - If the package is not found you may need to follow the directions [here](https://www.mongodb.com/docs/manual/tutorial/install-mongodb-on-ubuntu/).
- If you don't have a database location set up, set up a location:
> `mkdir -p ~/data/db/`
- In a terminal window or tab with the `ampliconenv` environment active, run MongoDB locally:
>  `mongod --dbpath ~/data/db` or `mongod --dbpath <DB_PATH>`

## 3a. View MongoDB data in MongoDB Compass
- Download MongoDB Compass: https://www.mongodb.com/docs/compass/current/install/#download-and-install-compass
- Open the MongoDB Compass app after starting MongoDB locally
- Connect to your local instance of MongoDB:
> URI: `mongodb://localhost:27017`
- Relevant data will be located in `/AmpliconRepository/projects/`
- You can periodically clear your local deployment mongodb files using Compass so that your disk does not fill up.
- Run `export DB_URI_SECRET='mongodb://localhost:27017'` in your terminal to set the environment variable for your local database.
  - So that this is active every time, you can add the command above to your `~/.bashrc` file
- Note that the latest version of Compass (1.34.2) won't work with our older DB version.  You can get an old compass for mac at https://downloads.mongodb.com/compass/mongodb-compass-1.28.4-darwin-x64.dmg

## 3b. Clearing your local DB
Periodically, you will want to purge old or excessively large accumulated data from you DB. You can do this using the provided script
> `python purge-local-db.py`

## 4. Neo4j Download Instructions

### Docker

the easiest way... edit the path at the end to the local drive you want it to use

```docker run -d --name neo4j -p 7474:7474 -p 7687:7687 --env NEO4J_AUTH=neo4j/$NEO4J_PASSWORD_SECRET -v /home/ubuntu/AmpliconRepository-dev/neo4j neo4j```<br>


### macOS

Download and unzip the tar file:<br>
```curl -O -C - http://dist.neo4j.org/neo4j-community-5.12.0-unix.tar.gz```<br>
```tar -xvzf neo4j-community-5.12.0-unix.tar.gz```<br>

Start neo4j with the console command:<br>
```cd neo4j-community-5.12.0```<br>
```bin/neo4j console```<br>

Go to http://localhost:7474/browser/ and change the auth settings. By default, both user and password are 'neo4j'. Keep user as 'neo4j' and change password to 'password'.

The environment is now set up. Ensure that neo4j is running before querying the graph.


> Alternatively, go to https://neo4j.com/deployment-center/, then download the rpm file for the latest Community Edition under the section titled 'Graph Database Self-Managed'. Further instructions are available upon clicking Download. Note that this method has not been tested by our team.

### Ubuntu (or Windows via WSL/WSL2)

Please follow this documentation to set up the latest version of [Neo4j Community Edition](https://neo4j.com/docs/operations-manual/2025.01/installation/linux/debian/)

In brief, you can do

```
wget -O - https://debian.neo4j.com/neotechnology.gpg.key | sudo apt-key add -
echo 'deb https://debian.neo4j.com stable latest' | sudo tee /etc/apt/sources.list.d/neo4j.list
sudo apt-get update
sudo apt-get install neo4j
```

Register for an account at [Neo4j Aura Console](https://console.neo4j.io/)

Then launch it by running
```
sudo neo4j start
```

Visit http://localhost:7474 and login with neo4j as both the user and password. You will be prompted to set a password for future use. 
You must set the updated password to the value in your `config.sh` file (value of NEO4J_PASSWORD_SECRET) 

For shutdown at the end of your session, you can do `sudo neo4j stop`

## 5. Set up secret keys for OAuth2 and other environment variables
- Make sure you have the `config.sh` file from another developer (this contains secret key information)
- Run the command to initialize variables:
`source config.sh`

For local deployments, you will need to ensure that the following two variables are set to FALSE, as shown below
```
export S3_STATIC_FILES=FALSE
export S3_FILE_DOWNLOADS='FALSE'
```

**IMPORTANT**: After recieving your `config.sh`, please ensure you do not upload it to Github or make it available publicly anywhere.


## 6. Run development server (Django)
- Open a terminal window or tab with the `ampliconenv` environment active
- Move to the `caper/` folder (should see `manage.py`)
- Run the server locally:
> `python manage.py runserver`
- Open the application on a web browser (recommend using a private/incognito window for faster development):
> https://localhost:8000

- Troublshooting tip: If you face an error that says port 8000 is already in use, you can kill all tasks using that port by doing
`sudo fuser -k 8000/tcp`

# Option B - Local deployment with Docker: <a name="dev-docker"></a>

These steps guide users on how to set up their development environment using Docker and `docker compose` as an alternative to python or conda-based package management and installation. **This is the simplest way to locally deploy the server for new users.** 

**Important:** You first need to install [docker>=20.10](https://docs.docker.com/engine/install/) on your machine.


To test the installation of Docker please do:

```bash
# check version: e.g. Docker version 20.10.8, build 3967b7d
docker --version

# check if compose module is present
docker compose --help

# check docker engine installation
sudo docker run hello-world
```


## Quickstart

Build and run your development webserver and mongo db using docker:

```bash
cd AmpliconRepository
# place config.sh in caper/, and place .env in current dir
# change UID and GID in .env to match the host configuration
# create all folders which you want to expose to the container
mkdir -p logs tmp .aws .git
docker compose -f docker-compose-dev.yml build --no-cache --progress=plain
docker compose -f docker-compose-dev.yml up -d
# then visit http://localhost:8000/ in your web browser
# once finished, to shutdown:
docker compose -f docker-compose-dev.yml down
```

## Complete steps

### i. Start your [docker daemon](https://docs.docker.com/config/daemon/start/) and make sure is running:

```bash
# for linux
sudo systemctl start docker
docker --help
docker compose --help

# or alternatively start manually from interface (macos or windows)
```

### ii. Clone the repository (skip this if you have already done this):

```bash
git clone https://github.com/AmpliconSuite/AmpliconRepository.git
```

### iii. Build a local Docker image:

This command will create a Docker image `genepattern/amplicon-repo:dev-test` with your environment, all dependencies and application code you need to run the webserver.
Additionally, this command will pull a `mongo:4` image for the test database. 

**First, obtain the secret files `.env` and `config.sh` from another developer**. Do not share these files with others outside the project. Do not upload them anywhere. Keep them private.

Next, Place `.env` under `AmpliconRepository/` and `config.sh` under `AmpliconRepository/caper/`.

You should see these required files:
- `docker-compose-dev.yml`
- `Dockerfile`
- `.env` 
- `requirements.txt`
- `caper/config.sh`

```bash
cd AmpliconRepository
docker compose -f docker-compose-dev.yml build --progress=plain --no-cache
```

### iv. Run webserver and mongo db instances: 

This command will:
- create two containers, one for the webserver (`amplicon-dev`) and one for the mongo database (`ampliconrepository_mongodb_1`)
- will use `.env` to configure all environment variables used by the webserver and mongodb 
- will start the webserver on `localhost:8000`
- will start a mongodb instance listening on port `27017`
- will mount a volume with your source code `-v ${PWD}:/srv/:rw`

```bash
# create all folders exposed to container
mkdir -p logs tmp .aws .git
# start container using the host UID and GID (change in .env)
docker compose -f docker-compose-dev.yml up -d
#[+] Running 2/2
# ⠿ Container ampliconrepository-mongodb-1  Started                                                                                                                           0.3s
# ⠿ Container amplicon-dev                  Started                                                                                                                           1.1s
```

To check if your containers are running do:

```bash
docker ps
```
and you should see something like below:
```
# CONTAINER ID   IMAGE                                COMMAND                   CREATED         STATUS              PORTS                      NAMES
# 311a560ec20a   genepattern/amplicon-repo:dev   "/bin/sh -c 'echo \"H…"   3 minutes ago   Up About a minute   0.0.0.0:8000->8000/tcp     amplicon-dev
# deaa521621f1   mongo:4                              "docker-entrypoint.s…"    4 days ago      Up About a minute   0.0.0.0:27017->27017/tcp   ampliconrepository_mongodb_1
```

To view the site locally, visit http://localhost:8000/ in your web browser.

### v. Stop webserver and mongodb

To stop the webserver and mongodb service:

```bash
docker compose -f docker-compose-dev.yml down
#[+] Running 3/2
# ⠿ Container amplicon-dev                  Removed                                                                                                                          10.3s
# ⠿ Container ampliconrepository-mongodb-1  Removed                                                                                                                           0.3s
# ⠿ Network ampliconrepository_default      Removed                                                                                                                           0.0s
```

### vi. Check environment configuration of your docker-compose

Before you build your image you can check if the `config.sh` is set correctly by doing:

```bash
docker compose -f docker-compose-dev.yml config
```

This command will show you the `docker-compose-dev.yml` customized with your environment variables.

### vii. Check environment variables for running container

You can check the environment variables which your running container uses:

```bash
docker inspect -f \
   '{{range $index, $value := .Config.Env}} {{$value}}{{println}}{{end}}' \
   container_id
```

### viii. Debug

- Run `docker ps` and check if the port mapping is correct, i.e. you should see `0.0.0.0:8000->8000`=`host_localip:host_port->docker_port`
- Port mapping annotation for `docker run -p 8000:8000 ...`=`HOST:DOCKER`
- For local development you need to use host port `8000` to be able to use the Google Authentication in the App
- Set `AMPLICON_ENV_PORT` if you want to use another port on the host machine, then rebuild the docker image.
- If you get the error `permission denied/read only database` please set the read-write permissions on your local machine to `777` for the following
`sudo chmod 777 logs/ tmp/ .aws/ caper/caper.sqlite3 -R`
- If you have an older version of docker `docker compose` may not be available and you will need to install `docker-compose` and use that, replacing `docker compose` with `docker-compose`.
- Error: `unix /var/run/docker.sock: connect: permission denied` -> [see](https://stackoverflow.com/questions/48568172/docker-sock-permission-denied)
- If you need to run as a non-root user (rare), please set `UID` and `GID` in your `.env` file to match the host `UID` `GID`, or run as so:
  `env UID=${UID} GID=${GID} docker compose -f docker-compose-dev.yml up`
- Make sure all folders which are mounted as volumes at runtime are created upfront (below for development):
  `cd AmpliconRepository; mkdir -p logs tmp .aws .git`
- My local `mongodb` instance is not running or you get `ampliconrepository-mongodb-1 exited with code 14`? Try do `cd AmpliconRepository; rm -rf data` and restart using `docker-compose`


# Testing datasets <a name="test-datasets"></a> 
[These datasets](https://drive.google.com/drive/folders/1lp6NUPWg1C-72CQQeywucwX0swnBFDvu?usp=share_link) are ready to upload to the site for testing purposes.

# Pushing changes to GitHub and merging PRs <a name="pr"></a> 
- Work on branches and open pull requests to merge changes into main.
- **Please ensure that you do not commit `caper.sqlite3` along with your other changes.** 
- PR reviewers, please check that `caper.sqlite3` is not among the changed files in a PR.
- When merging a PR, please do the following steps:
  - pull the branch in question and load in local deployment
  - at minimum, test the following pages to see if everything looks right:
    - home page
    - CCLE project page
    - load a random sample in CCLE

# Using the development server <a name="dev-server"></a> 
- Please see the [wiki page on using the development server](https://github.com/mesirovlab/AmpliconRepository/wiki/dev.ampliconrepository.org-instructions).

# Logging in as admin <a name="admin-logging"></a> 
 - Please see the [wiki page on admin login](https://github.com/mesirovlab/AmpliconRepository/wiki/Becoming-Admin-on-a-development-server).

# How to deploy and update the production server for AmpliconRepository <a name="deploy"></a> 
The server is currently running on an EC2 instance through Docker. The ports active on HTTP and HTTPS through AWS Load Balancer. There are two main scripts to start and stop the server.

**Note:** While we provide a Dockerfile, local deployment of the site using the docker will only properly work on AWS. Local deployment should be done with a local install using the steps above.



## 1. How to start the server
- SSH into the EC2 instance (called `ampliconrepo-ubuntu-20.04`)
  - this requires a PEM key
- Go to project directory
> `cd /home/ubuntu/caper/`
- Check to see if the Docker container is running
> `docker ps` (look for a container called amplicon)
- If it is not running, run the start script 
> `./start-server.sh`

## 2. How to stop the server
- SSH into the EC2 instance (called `ampliconrepo-ubuntu-20.04`)
  - this requires a PEM key
- Go to project directory
> `cd /home/ubuntu/caper/`
- Check to see if the Docker container is running
> `docker ps` (look for a container called amplicon)
- If it is running, run the stop script 
> `./stop-server.sh`

## 3. How to update the server

- Clone repo using https, ssh, or GitHub Desktop to your local machine
- Make changes locally 
- Push changes to the main branch of the repository
- Create a release on GitHub
    -   login to github and go to the releases page at https://github.com/AmpliconSuite/AmpliconRepository/releases.
    -   Create a new release using a tag with the pattern v<major version>.<minor version>.<patch version>_<MMDDYY>  e.g. v1.0.1_072523 for version 1.0.1 created July 15, 2023.
    - This will create a tag on the contents of the repo at this moment
    - a github action will update and commit the version.txt file with the date, tag, commit ID and person doing the release and apply the tag to the updated version.txt
- SSH into the EC2 instance (called `ampliconrepo-ubuntu-20.04` in us-east-1)
  - this requires a PEM key
- Go to project directory
> `cd /home/ubuntu/AmpliconRepository-prod/`
> `source caper/config.sh`
- Pull your changes from Github
> `git fetch`
> `git pull`
> `git checkout tags/<release tag in github>`
- Restart the server
> `./stop-server.sh`
> `./start-server.sh`


