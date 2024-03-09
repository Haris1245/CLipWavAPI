from flask import Flask, jsonify, request
from pytube import YouTube
import boto3
from moviepy.editor import *
from openai import OpenAI
from moviepy.video.tools.subtitles import SubtitlesClip
import random
import io
import tempfile
import requests
app = Flask(__name__)
BUCKET_NAME = "bucket_name"
client = OpenAI(api_key="OPENAI_API_KEY")

colors = ["green", "yellow", "red", "white"]
fonts = ["Impact", "Comic-Sans-MS-Bold"]

def upload_to_s3(filename, video_stream):
    s3_client = boto3.client("s3")
    with requests.get(video_stream.url, stream=True) as response:
        if response.status_code == 200:
            s3_client.upload_fileobj(io.BytesIO(response.content), BUCKET_NAME, filename)

def get_subs(clip, key_name):
    audio = clip.audio
    audio_bytes = io.BytesIO()
    audio.write_audiofile(audio_bytes, codec='pcm_s16le')  # Write audio to BytesIO object

    try:
        # Get the bytes content from BytesIO
        audio_data = audio_bytes.getvalue()

        # Upload audio data directly to S3
        upload_audio_to_s3(audio_data, BUCKET_NAME, f"audio_{key_name}")

        # Transcribe audio using OpenAI's API
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_data,  # Pass the retrieved bytes data
            response_format="verbose_json",
            timestamp_granularities=["word"]
        )

        text = transcript.text
        timestamps = transcript.words

        return {'text': text, 'timestamps': timestamps}

    except Exception as e:
        print("An error occurred during processing:", e)
        raise


def upload_audio_to_s3(audio_bytes, bucket_name, key_name):
    s3 = boto3.client('s3')
    s3.put_object(Body=audio_bytes, Bucket=bucket_name, Key=key_name)

def get_video_url(filename):
    s3_client = boto3.client("s3")
    s3_object = s3_client.get_object(Bucket=BUCKET_NAME, Key=filename)
    
    if 'Location' in s3_object:
        video_url = s3_object["Location"]
        return video_url
    else:
        region = s3_client.meta.region_name
        bucket_url = f"https://{BUCKET_NAME}.s3.{region}.amazonaws.com/"
        video_url = bucket_url + filename
        return video_url

def generate_subtitles_clip(subs, delay=0.05):
    text = subs['text']
    timestamps = subs['timestamps']
    
    clips = []
    for word_info in timestamps:
        start_time = word_info['start'] + delay
        end_time = word_info['end'] + delay
        word = word_info['word']
        clips.append(((start_time, end_time), word.upper()))
    
    font = random.choice(fonts)
    color = random.choice(colors)
    return SubtitlesClip(clips, lambda txt: TextClip(txt, fontsize=100, color=color, method='caption', stroke_color="black", stroke_width=6, font=font))

def upload_video_to_s3(video_bytes, bucket_name, key_name):
    s3 = boto3.client('s3')
    s3.put_object(Body=video_bytes, Bucket=bucket_name, Key=key_name)

@app.route('/make-short', methods=['GET'])
def make_short():
    s3 = boto3.client('s3')
    link = request.args.get('link')
    start = request.args.get('start')
    end = request.args.get('end')

    if link and start and end:
        try:
            youtube_object = YouTube(link)
            video_stream = youtube_object.streams.get_highest_resolution()
            filename = video_stream.default_filename.replace(' ', '')
            upload_to_s3(filename, video_stream)
            video_url = get_video_url(filename)
            video = VideoFileClip(video_url).subclip(start, end).fx(vfx.fadeout, 1)
            aspect_ratio = video.size[0] / video.size[1]

            if aspect_ratio > 9/16:  # Video is wider than 9:16
                new_width = int(9/16 * video.size[1])
                crop_x = (video.size[0] - new_width) / 2
                crop_y = 0
                video = video.crop(x1=crop_x, y1=crop_y, x2=crop_x + new_width, y2=video.size[1])
            else:  # Video is taller than 9:16
                new_height = int(16/9 * video.size[0])
                crop_x = 0
                crop_y = (video.size[1] - new_height) / 2
                video = video.crop(x1=crop_x, y1=crop_y, x2=video.size[0], y2=crop_y + new_height)


            # Generate subtitles and create SubtitlesClip
            subs_result = get_subs(video, f"subs_{filename}")
            subs_clip = generate_subtitles_clip(subs_result)

            # Overlay subtitles on the video and write the final video file to a temporary file
            final_video = CompositeVideoClip([video.set_duration(subs_clip.duration), subs_clip.set_position(((1920/2 - 1080/2), 1200))])
            temp_video_path = tempfile.NamedTemporaryFile(suffix='.mp4').name
            final_video.write_videofile(temp_video_path, codec="libx264")

            # Upload final video to S3 and clean up uploaded files
            with open(temp_video_path, 'rb') as temp_video_file:
                video_bytes = temp_video_file.read()
                upload_video_to_s3(video_bytes, BUCKET_NAME, f"{filename}_short")
            
            s3.delete_object(Bucket=BUCKET_NAME, Key=filename)
            s3.delete_object(Bucket=BUCKET_NAME, Key=f"subs_{filename}")

            url = get_video_url(f"{filename}_short")
            return jsonify({"message": "Video uploaded to S3 successfully!", "url": url})
        
        except Exception as e:
            print("An error occurred:", e)
            return jsonify({"message": "Error downloading or uploading video"}), 500
    
    else:
        return jsonify({"message": "Missing parameters: 'link', 'start', 'end'"}), 400

if __name__ == "__main__":
    app.run(port=3000, debug=True)
