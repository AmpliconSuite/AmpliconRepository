# AmpliconRepository

#### Authors: Forrest Kim, Jens Luebeck, Ted Liefeld, Edwin Huang, Thorin Tabor, Vineet Bafna
---

This is the main repository for the AmpliconRepository.

# How to set up a development server for AmpliconRepository

## Requirements
- Python Virtual Environment (3.8 or higher)

## 1. Clone the repository from GitHub
- Clone repo using https, ssh, or GitHub Desktop to your local machine

## 2. Set up the virtual environment and install packages:
- In a terminal window, move to the cloned Github repo
- Go to the caper top level directory (should see `requirements.txt`)
#### Option A: Using python's environment manager
- Create a new Python virtual environment:
> `python -m venv ampliconenv`
- Activate the new environment (you need to do this everytime before running the server):
> `source ampliconenv/bin/activate`
- Install required packages
> `pip install -r requirements.txt`
#### Option B: Using conda's environment manager
- Create a new Conda environment
> `conda create -n "ampliconenv" python=3.8.0`
- To activate
> `conda activate ampliconenv`
- Install required packages
> `pip install -r requirements.txt`



## 3. Set up MongoDB locally (for development)
- Install MongoDB
  - In Ubuntu this can be done with `sudo apt install mongodb-server-core`
- If you don't have a database location set up, set up a location:
> `mkdir -p ~/data/db/`
- In a terminal window or tab with the `caperenv` environment active, run MongoDB locally:
>  `mongod --dbpath ~/data/db` or `mongod --dbpath <DB_PATH>`

## 3a. View MongoDB data in MongoDB Compass (Optional)
- Download MongoDB Compass: https://www.mongodb.com/docs/compass/current/install/#download-and-install-compass
- Open the MongoDB Compass app after starting MongoDB locally
- Connect to your local instance of MongoDB:
> URI: `mongodb://localhost:27017`
- Relevant data will be located in `/caper/projects/`
- Run `export DB_URI='mongodb://localhost:27017'` in your terminal to set the environment variable for your local database.
  - So that this is active every time, you can add the command above to your `~/.bashrc` file
- Note that the latest version of Compass (1.34.2) won't work with our older DB version.  You can get an old compass for mac at https://downloads.mongodb.com/compass/mongodb-compass-1.28.4-darwin-x64.dmg

## 4. Set up secret keys for OAuth2
- Make sure you have the `config.sh` file from another developer (this contains secret key information)
- Run the command to initialize variables:
`source config.sh`

## 5. Run development server (Django)
- Open a terminal window or tab with the `caperenv` environment active
- Move to the caper App folder (should see `manage.py`)
- Run the server locally:
> `python manage.py runserver`
- Open the application on a web browser (recommend using a private/incognito window for faster development):
> https://localhost:8000


# How to deploy and update the server for AmpliconRepository
The server is currently running on an EC2 instance through Docker. The ports active on HTTP and HTTPS through AWS Load Balancer. There are two main scripts to start and stop the server.

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


