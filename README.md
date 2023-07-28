# AmpliconRepository

#### Authors: Forrest Kim, Jens Luebeck, Ted Liefeld, Edwin Huang, Gino Prasad, Rohil Ahuja, Rishaan Kenkre, Tushar Agashe, Devika Torvi, Thorin Tabor, Vineet Bafna
---

This is the main repository for the AmpliconRepository.

- [How to set up a development server for AmpliconRepository](#aa-env-install)
- [Setup your development environment using Docker and docker-compose](#dev-docker)
- [Testing datasets](#test-datasets) 
- [Pushing changes to GitHub and merging PRs](#pr)
- [Using the development server](#dev-server)
- [Logging in as admin](#admin-logging)
- [How to deploy and update the production server for AmpliconRepository](#deploy)


# How to set up a development server for AmpliconRepository <a name="aa-env-install"></a> 

## Requirements
- Python Virtual Environment (3.8 or higher)

## 1. Clone the repository from GitHub
- Clone repo using https, ssh, or GitHub Desktop to your local machine

## 2. Set up the virtual environment and install packages:
- In a terminal window, move to the cloned Github repo
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
> `[/path/to/your/conda]/envs/ampliconenv/bin/pip install -r requirements.txt`



## 3. Set up MongoDB locally (for development)
- Install MongoDB
  - In Ubuntu this can be done with `sudo apt install mongodb-server-core`
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
- Run `export DB_URI='mongodb://localhost:27017'` in your terminal to set the environment variable for your local database.
  - So that this is active every time, you can add the command above to your `~/.bashrc` file
- Note that the latest version of Compass (1.34.2) won't work with our older DB version.  You can get an old compass for mac at https://downloads.mongodb.com/compass/mongodb-compass-1.28.4-darwin-x64.dmg

## 4. Set up secret keys for OAuth2
- Make sure you have the `config.sh` file from another developer (this contains secret key information)
- Run the command to initialize variables:
`source config.sh`

## 5. Run development server (Django)
- Open a terminal window or tab with the `ampliconenv` environment active
- Move to the `caper/` folder (should see `manage.py`)
- Run the server locally:
> `python manage.py runserver`
- Open the application on a web browser (recommend using a private/incognito window for faster development):
> https://localhost:8000

# Local deployment - Docker option <a name="dev-docker"></a>

Set up your development environment using Docker and docker-compose as an alternative to python or conda-based package management and installation. 

To use containers for development you need to install [Docker](https://docs.docker.com/engine/install/) and [docker-compose](https://docs.docker.com/compose/install/) on your machine.

Instructions for Ubuntu:
- Follow instructions at https://docs.docker.com/engine/install/ubuntu/#set-up-the-repository
then do
- `sudo apt-get install docker-compose`

## a. Quickstart

Build and run your development webserver and mongo db using docker:

```bash
cd AmpliconRepository
docker compose -f docker-compose-dev.yml build
docker compose -f docker-compose-dev.yml up -d
# then visit http://localhost:8000/ in your web browser
# once finished, to shutdown:
docker compose -f docker-compose-dev.yml down
```

## b. Complete steps

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

```bash

Dependency files:
- `docker-compose-dev.yml`
- `Dockerfile_dev`
- `.env` # ask AmpRepo devs for the contents of this file
- `environment-dev.yml`
- `requirements.txt`
- `caper/config.sh` # ask AmpRepo devs for the contents of this file & place in caper/ dir.

cd AmpliconRepository
docker compose -f docker-compose-dev.yml build
```

### iv. Run webserver and mongo db instances: 

This command will:
- create two containers, one for the webserver (`amplicon-repo`) and one for the mongo database (`ampliconrepository_mongodb_1`)
- will use `.env` to configure all environment variables used by the webserver and mongodb 
- will start the webserver on `localhost:8000`
- will start a mongodb instance listening on port `27017`
- will mount a volume with your source code `-v ${PWD}:/home/${AA_USER}/code`



```bash
docker compose -f docker-compose-dev.yml up -d
# Starting ampliconrepository_mongodb_1 ... done
# Starting amplicon-repo                ... done
```

To check if your containers are running do:

```bash
docker ps
```
and you should see something like below:
```
# CONTAINER ID   IMAGE                                COMMAND                   CREATED         STATUS              PORTS                      NAMES
# 311a560ec20a   genepattern/amplicon-repo:dev-test   "/bin/sh -c 'echo \"H…"   3 minutes ago   Up About a minute   0.0.0.0:8000->8000/tcp     amplicon-repo
# deaa521621f1   mongo:4                              "docker-entrypoint.s…"    4 days ago      Up About a minute   0.0.0.0:27017->27017/tcp   ampliconrepository_mongodb_1
```

To view the site locally, visit http://localhost:8000/ in your web browser.

### v. Stop webserver and mongodb

To stop the webserver and mongodb service:

```bash
docker compose -f docker-compose-dev.yml down
# Stopping amplicon-repo                ... done
# Stopping ampliconrepository_mongodb_1 ... done
# Removing amplicon-repo                ... done
# Removing ampliconrepository_mongodb_1 ... done
# Removing network ampliconrepository_default
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
   container_name
```

### viii. Debug

- Run `docker ps` and check if the port mapping is correct, i.e. you should see `0.0.0.0:8000->8000`=`inside_docker_port->host_port`
- Please be aware that the above port mapping annotation is different from `docker run -p 8000:8000 ...`=`HOST:DOCKER`
- For local development you need to use host port `8000` to be able to use the Google Authentication in the App
- Set `AMPLICON_ENV_PORT` if you want to use another port on the host machine, then rebuild the docker image.
- If you get the error `permission denied/read only database` please set the read-write permissions on your local machine to `777` for the following
`sudo chmod 777 logs/ tmp/ .aws/ caper/caper.sqlite3 -R`
- If you have an older version of docker `docker compose` may not be available and you will need to install `docker-compose` and use that, replacing `docker compose` with `docker-compose`.

# Testing datasets <a name="test-datasets"></a> 
[These datasets](https://drive.google.com/drive/folders/1lp6NUPWg1C-72CQQeywucwX0swnBFDvu?usp=share_link) are ready to upload to the site for testing purposes.

# Pushing changes to GitHub and merging PRs <a name="pr"></a> 
- Work on branches and open pull requests to merge changes into main.
- Please ensure that you do not commit `caper.sqlite3` along with your other changes. 
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
- SSH into the EC2 instance (called `ampliconrepo-ubuntu-20.04`)
  - this requires a PEM key
- Go to project directory
> `cd /home/ubuntu/caper/`
- Pull your changes from Github
> `git pull origin main`
- Restart the server
> `./stop-server.sh`
> `./start-server.sh`


