A python script to help download works from AO3 in bulk. It takes in a folder full of .txt files and will download every work in those .txt files. It was created as a substitute for Calibre's FanFicFare plugin, which does not work when AO3 is protected with Cloudflare.

### Prerequisites
- Python installed on your PC
- https://github.com/nianeyna/ao3downloader. See the repo instructions for how to get it running. Pay attention to the Python version that you need to run ao3downloader in their README (Python 3.11.4). Once you have ao3downloader running, continue with the steps below.

1. 

### Step 1: Grabbing Links
Use ao3downloader to grab all work urls from ao3 search result pages.

1. clone https://github.com/nianeyna/ao3downloader
2. `cd ao3downloader`
3. 
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
4. `python ao3downloader.py`
5. enter option `l: get all work links from an ao3 listing (saves links only)` in the menu
6. when it asks for an ao3 link, enter the link to your ao3 search results page. Ex. "https://archiveofourown.org/works?commit=Sort+and+Filter&work_search%5Bsort_column%5D=hits&include_work_search%5Brating_ids%5D%5B%5D=10&include_work_search%5Bcategory_ids%5D%5B%5D=23&work_search%5Bother_tag_names%5D=Alternate+Universe&work_search%5Bexcluded_tag_names%5D=&work_search%5Bcrossover%5D=&work_search%5Bcomplete%5D=T&work_search%5Bwords_from%5D=10000&work_search%5Bwords_to%5D=&work_search%5Bdate_from%5D=&work_search%5Bdate_to%5D=&work_search%5Bquery%5D=&work_search%5Blanguage_id%5D=en&tag_id=%EB%B0%A9%ED%83%84%EC%86%8C%EB%85%84%EB%8B%A8+%7C+Bangtan+Boys+%7C+BTS"
7. if it asks you to login, login if you have an account. You dont need to though.
8. Keep doing this for various search results

At the end of this process, you should have a bunch of .txt files in ao3downloader/downloads that are filled with work links.
### Step 2: Downloading Links
Use the script in this repo to download works in bulk. It requires python. It will take in a folder full of `.txt` files and download them all.

1. clone this repository and cd into it
2. 
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
3. create a folder named `links` in the same directory as this repository
4. place all of the txt files from step 1 into the links folder
5. `python download.py`

Donwloading ~4k works took 6 hours because of the pauses I placed in the script. You may edit the pause duration at your own risk. Shorter values could work, I never tested them. No pauses may cause you to get rate limited or IP banned by AO3.