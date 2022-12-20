# AmpliconRepository

#### Authors: Forrest Kim, Jens Luebeck, Ted Liefeld, Edwin Huang, Thorin Tabor, Vineet Bafna
---

This is the main repository for the AmpliconRepository. Currently in development. 

# How to set up the development server for AmpliconRepository

## Requirements
- Anaconda (https://www.anaconda.com/products/distribution)

## 1. Clone the repository from GitHub
- Clone repo using https, ssh, or GitHub Desktop to your local machine

## 2. Set up the virtual environment and install packages:
- In a terminal window, move to the cloned Github repo
- Go to the caper top level directory (should see `environment.yml` and `requirements.txt`)
- Create a new Anaconda environment:
> `conda env create -f environment.yml`
- Activate the new environment (you need to do this everytime before running the server):
> `conda activate caperenv` 

## 3. Set up MongoDB locally (for development)
- If you don't have a database location set up, set up a location:
> `mkdir ~/data/db/`
- In a terminal window or tab with the `caperenv` environment active, run MongoDB locally:
>  `mongod --dbpath ~/data/db` or `mongod --dbpath <DB_PATH>`

## 3a. View MongoDB data in MongoDB Compass (Optional)
- Download MongoDB Compass: https://www.mongodb.com/docs/compass/current/install/#download-and-install-compass
- Open the MongoDB Compass app after starting MongoDB locally
- Connect to your local instance of MongoDB:
> URI: `mongodb://localhost:27017`
- Relevant data will be located in `/caper/projects/`
- Note that the latest version of Compass (1.34.2) won't work with our older DB version.  You can get an old compass for mac at https://downloads.mongodb.com/compass/mongodb-compass-1.28.4-darwin-x64.dmg

## 4. Set up secret keys for OAuth2
- Open a terminal window or tab with the `caperenv` environment active
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
