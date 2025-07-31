import requests
import os
import time
import logging
from glob import glob
from datetime import datetime

def setup_logging():
    logging.basicConfig(filename='download_errors.log', level=logging.ERROR, 
                        format='%(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def get_total_links(directory_path):
    total_links = 0
    for file_path in glob(os.path.join(directory_path, '*.txt')):
        with open(file_path, 'r') as file:
            total_links += sum(1 for line in file)
    return total_links

def get_failed_links():
    """Reads download_errors.log and returns a set of failed URLs."""
    if not os.path.exists('download_errors.log'):
        return set()
    
    with open('download_errors.log', 'r') as log_file:
        return {line.strip() for line in log_file}

def download_stories_from_file(file_path, download_folder, total_links, completed_downloads, failed_links):
    downloads_counter = 0
    with open(file_path, 'r') as file:
        for line in file:
            original_url = line.strip()
            if original_url in failed_links:
                print(f"Skipping previously failed URL: {original_url}")
                completed_downloads[0] += 1
                continue

            download_url = original_url.replace("https://", "https://download.").replace("/works/", "/downloads/") + "/download.epub"
            story_id = download_url.split('/')[-2]
            file_name = f"{story_id}.epub"
            file_path = os.path.join(download_folder, file_name)
            
            # Check if file already exists
            if os.path.exists(file_path):
                print(f"{file_name} already exists. Skipping download.")
                completed_downloads[0] += 1
                continue
            
            try:
                print(f"Downloading {story_id}.")
                cookie = {"_otwarchive_session":"your_cookie_here"}
                response = requests.get(download_url, cookies=cookie, timeout=20)
                response.raise_for_status()
                with open(file_path, 'wb') as f:
                    f.write(response.content)

                print(f"Downloaded {file_name} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                downloads_counter += 1
                completed_downloads[0] += 1
                
                # Pause every 20 downloads for 60 seconds
                if downloads_counter % 25 == 0:
                    print(f"Pausing for 60 seconds... Downloaded {completed_downloads[0]} out of {total_links} total files.")
                    time.sleep(120)
            except requests.RequestException as e:
                logging.error(original_url)
                print(f"Failed to download {file_name}: {e}")
                completed_downloads[0] += 1

def download_stories(directory_path, download_folder):
    if not os.path.exists(download_folder):
        os.makedirs(download_folder)
    
    total_links = get_total_links(directory_path)
    completed_downloads = [0] 
    failed_links = get_failed_links()  # Load failed links

    for file_path in glob(os.path.join(directory_path, '*.txt')):
        print(f"Processing {file_path}...")
        download_stories_from_file(file_path, download_folder, total_links, completed_downloads, failed_links)

setup_logging()
download_stories('./links', './downloaded')
