# AmpliconRepository

#### Authors: Forrest Kim, Jens Luebeck, Ted Liefeld, Edwin Huang, Thorin Tabor, Vineet Bafna
---

This is the main development 

# How to set up the Development Server for AmpliconRepository

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
> `cd ~`
> `mkdir data/db/`
- In a terminal window or tab with the `caperenv` environment active, run MongoDB locally:
> `mongod`

## 3a. View MongoDB data in MongoDB Compass (Optional)
- Download MongoDB Compass: https://www.mongodb.com/docs/compass/current/install/#download-and-install-compass
- Open the MongoDB Compass app after starting MongoDB locally
- 

## 4. Run development server (Django)
- Open a terminal window or tab with the `caperenv` environment active
- Move to the caper App folder (should see `manage.py`)
- Run the server locally:
> `python manage.py runserver`
- Open the application on a web browser:
> https://localhost:8000