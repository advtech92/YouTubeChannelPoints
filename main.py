import os
import sqlite3
import time
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone

# Constants
SCOPES = ['https://www.googleapis.com/auth/youtube.readonly']
DATABASE_FILE = 'channel_points.db'

# YouTube Channel ID or Handle
CHANNEL_HANDLE = 'UCsVJcf4KbO8Vz308EKpSYxw'
STREAM_KEYWORD = "Live"  # Keyword to identify the correct live stream

def get_authenticated_service():
    flow = InstalledAppFlow.from_client_secrets_file(
        'client_secret.json', SCOPES)
    creds = flow.run_local_server(port=63355)
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    return build('youtube', 'v3', credentials=creds)


def create_database():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS points (
            user_id TEXT PRIMARY KEY,
            points INTEGER DEFAULT 0,
            last_interaction TIMESTAMP,
            subscription_status TEXT,
            first_seen_as_member TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def add_points(user_id, points_to_add, subscription_status, interacted):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Determine the multiplier based on subscription status
    if subscription_status == "year_or_more":
        bonus_multiplier = 3
    elif subscription_status == "subscribed":
        bonus_multiplier = 2
    else:
        bonus_multiplier = 1

    if interacted:
        points_to_add += 5  # 5 extra points for interaction
    
    points_to_add *= bonus_multiplier

    cursor.execute('''
        INSERT INTO points (user_id, points)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET points = points + ?, last_interaction = ?
    ''', (user_id, points_to_add, points_to_add, datetime.utcnow()))
    conn.commit()
    conn.close()

    print(f"User {user_id} earned {points_to_add} points. Subscription status: {subscription_status}. Multiplier applied: {bonus_multiplier}")


def get_channel_id(youtube, handle):
    request = youtube.channels().list(
        part="id",
        forUsername=handle if handle.startswith("@") else None,
        id=handle if not handle.startswith("@") else None
    )
    response = request.execute()
    items = response.get('items', [])
    return items[0]['id'] if items else None


def get_channel_uploads_playlist_id(youtube, channel_id):
    request = youtube.channels().list(
        part="contentDetails",
        id=channel_id
    )
    response = request.execute()
    items = response.get('items', [])
    if items:
        return items[0]['contentDetails']['relatedPlaylists']['uploads']
    return None


def find_correct_live_video(youtube, channel_id, keyword):
    request = youtube.search().list(
        part="snippet",
        channelId=channel_id,
        eventType="live",
        type="video"
    )
    response = request.execute()
    items = response.get('items', [])
    for item in items:
        title = item['snippet']['title']
        if keyword.lower() in title.lower():
            return item['id']['videoId']
    return None


def is_video_live(youtube, video_id):
    request = youtube.videos().list(
        part="snippet,liveStreamingDetails",
        id=video_id
    )
    response = request.execute()
    items = response.get('items', [])
    if not items:
        return False

    snippet = items[0]['snippet']
    live_details = items[0].get('liveStreamingDetails', {})

    # Ensure the video is currently live
    if snippet.get('liveBroadcastContent') == 'live':
        actual_start_time = live_details.get('actualStartTime')
        actual_end_time = live_details.get('actualEndTime')

        if actual_start_time and not actual_end_time:
            return True

    return False


def get_live_chat_id(youtube, video_id):
    request = youtube.videos().list(
        part="liveStreamingDetails",
        id=video_id
    )
    response = request.execute()
    items = response.get('items', [])
    if items:
        live_chat_id = items[0]['liveStreamingDetails'].get('activeLiveChatId')
        print(f"Live Chat ID: {live_chat_id}")
        return live_chat_id
    return None


def monitor_chat(youtube, live_chat_id):
    if not live_chat_id:
        print("No valid live chat ID found.")
        return False

    next_page_token = None

    while True:
        try:
            request = youtube.liveChatMessages().list(
                liveChatId=live_chat_id,
                part="snippet,authorDetails",
                maxResults=200,
                pageToken=next_page_token
            )
            response = request.execute()

            if 'items' in response and response['items']:
                for item in response['items']:
                    user_id = item['authorDetails']['channelId']
                    display_name = item['authorDetails']['displayName']
                    is_moderator = item['authorDetails']['isChatModerator']
                    is_member = item['authorDetails']['isChatSponsor']  # Paid membership badge
                    member_since = item['authorDetails'].get('memberSince', None)  # Member since date (if available)
                    message = item['snippet']['displayMessage']
                    published_time = datetime.strptime(item['snippet']['publishedAt'], '%Y-%m-%dT%H:%M:%S.%f%z')

                    print(f"[{published_time}] {display_name}: {message} | Member: {is_member} | Member Since: {member_since}")

                    if not is_moderator:
                        conn = sqlite3.connect(DATABASE_FILE)
                        cursor = conn.cursor()
                        cursor.execute("SELECT last_interaction, subscription_status, first_seen_as_member FROM points WHERE user_id = ?", (user_id,))
                        result = cursor.fetchone()
                        
                        if result:
                            last_interaction, subscription_status, first_seen_as_member = result

                            if isinstance(first_seen_as_member, str):
                                first_seen_as_member = datetime.fromisoformat(first_seen_as_member)

                            if first_seen_as_member is None and is_member:
                                first_seen_as_member = published_time
                                cursor.execute('''
                                    UPDATE points
                                    SET first_seen_as_member = ?
                                    WHERE user_id = ?
                                ''', (first_seen_as_member, user_id))
                                conn.commit()

                            if first_seen_as_member and is_member:
                                membership_duration = datetime.now(timezone.utc) - first_seen_as_member
                                if membership_duration.days >= 365:
                                    subscription_status = "year_or_more"
                                else:
                                    subscription_status = "subscribed"

                        else:
                            if is_member:
                                subscription_status = "subscribed"
                                first_seen_as_member = published_time
                            else:
                                subscription_status = "none"
                                first_seen_as_member = None

                            cursor.execute('''
                                INSERT INTO points (user_id, points, last_interaction, subscription_status, first_seen_as_member)
                                VALUES (?, 0, NULL, ?, ?)
                            ''', (user_id, subscription_status, first_seen_as_member))
                            conn.commit()

                        print(f"User {user_id} subscription status set to: {subscription_status}, member_since: {first_seen_as_member}")

                        interacted = True

                        if interacted:
                            add_points(user_id, 10, subscription_status, interacted)

                        conn.close()

                next_page_token = response.get('nextPageToken')
                print(f"Next page token: {next_page_token}")

            else:
                print("No new messages detected; continuing to poll...")

        except Exception as e:
            print(f"Error while monitoring chat: {e}")
            time.sleep(30)  # Wait before retrying in case of an error
        
        time.sleep(10)  # Adjust this delay as needed


def set_membership_duration(user_id, months):
    """Manually set a user's membership duration."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Calculate the membership start date
    start_date = datetime.now(timezone.utc) - timedelta(days=months * 30)

    cursor.execute('''
        UPDATE points
        SET first_seen_as_member = ?, subscription_status = ?
        WHERE user_id = ?
    ''', (start_date, "year_or_more" if months >= 12 else "subscribed", user_id))

    conn.commit()
    conn.close()

    print(f"Manually set {user_id}'s membership start date to {start_date} ({months} months ago).")


def main():
    youtube = get_authenticated_service()
    create_database()

    # Example manual update
    set_membership_duration("UCfAxcCBuGbLqo-OjPr690Jg", 9)  # Example user with 9 months of membership

    channel_id = get_channel_id(youtube, CHANNEL_HANDLE)
    if not channel_id:
        print("Channel ID not found!")
        return

    video_id = find_correct_live_video(youtube, channel_id, STREAM_KEYWORD)
    if video_id and is_video_live(youtube, video_id):
        print("Correct live stream found and is live!")
        live_chat_id = get_live_chat_id(youtube, video_id)
        if live_chat_id:
            print("Monitoring chat...")
            monitor_chat(youtube, live_chat_id)
        else:
            print("No live chat ID available.")
    else:
        print("Could not find the correct live stream or it is not live.")

    time.sleep(300)  # Check every 5 minutes


if __name__ == "__main__":
    main()
