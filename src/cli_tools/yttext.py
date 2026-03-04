import sys
import re
import requests
from youtube_transcript_api import YouTubeTranscriptApi


def extract_video_id(url):
    if re.fullmatch(r'[a-zA-Z0-9_-]{11}', url):
        return url
    match = re.search(r'(?:v=|\/v\/|youtu\.be\/)([a-zA-Z0-9_-]{11})', url)
    return match.group(1) if match else None


def fetch_video_title(video_id):
    resp = requests.get(f'https://www.youtube.com/watch?v={video_id}')
    match = re.search(r'<title>(.+?) - YouTube</title>', resp.text)
    if match:
        return match.group(1).strip()
    return video_id


def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', '', name).strip()


def fetch_transcript(video_id):
    ytt = YouTubeTranscriptApi()
    transcript = ytt.fetch(video_id)
    return ' '.join(entry.text for entry in transcript)


def main():
    if len(sys.argv) != 2:
        print(f'Usage: yttext <youtube_url>')
        sys.exit(1)

    url = sys.argv[1]
    video_id = extract_video_id(url)
    if not video_id:
        print(f'Could not extract video ID from: {url}')
        sys.exit(1)

    title = fetch_video_title(video_id)
    text = fetch_transcript(video_id)
    filename = f'{sanitize_filename(title)}.txt'
    with open(filename, 'w') as f:
        f.write(text)

    print(filename)


if __name__ == '__main__':
    main()
