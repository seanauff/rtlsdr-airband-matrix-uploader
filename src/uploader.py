import os
import asyncio
import re
from nio import AsyncClient, UploadError, RoomSendError, RoomCreateError, RoomVisibility
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import logging

class UploadHandler(FileSystemEventHandler):
    def __init__(self, client, room_ids, upload_queue):
        self.client = client
        self.room_ids = room_ids
        self.upload_queue = upload_queue  # Queue to pass tasks to the main thread

    async def upload_file(self, file_path: str, room_id: str):
        """
        Upload an audio file to the Matrix media repository and send it as a message to a room.

        Args:
            file_path (str): The path to the audio file to upload.
            room_id (str): The ID of the Matrix room to send the file to.
        """
        logging.info(f"Uploading {file_path} to room {room_id}")
        # Add a 1-second delay to ensure the file is fully written
        await asyncio.sleep(1)

        try:
            # Open the file in binary read mode
            with open(file_path, "rb") as f:
                # Upload the file to the Matrix media repository
                upload_response = await self.client.upload(
                    file=f,
                    content_type="audio/mpeg",  # MIME type for MP3 audio files
                    filename=os.path.basename(file_path),  # Use the file's basename as the name
                    encrypt=False  # Assuming no encryption for simplicity
                )

                # Check if the upload failed
                if isinstance(upload_response, UploadError):
                    logging.error(f"Failed to upload file: {upload_response.message}")
                    return

                # Get the MXC URI of the uploaded file
                mxc_uri = upload_response.content_uri
                logging.info(f"File uploaded successfully: {mxc_uri}")

                # Prepare the content for the audio message
                content = {
                    "msgtype": "m.audio",  # Message type for audio files
                    "body": os.path.basename(file_path),  # File name as the message body
                    "url": mxc_uri  # URL of the uploaded file
                }

                # Send the message to the specified room
                send_response = await self.client.room_send(
                    room_id=room_id,
                    message_type="m.room.message",  # Standard message event type
                    content=content
                )

                # Check if sending the message failed
                if isinstance(send_response, RoomSendError):
                    logging.error(f"Failed to send message: {send_response.message}")
                else:
                    logging.info(f"Successfully sent message for file: {file_path}")

        except FileNotFoundError:
            logging.error(f"File not found: {file_path}")
        except Exception as e:
            logging.error(f"Error in upload_file: {e}")

    # Example of how this might be triggered (for context)
    def on_moved(self, event):
        """
        Handle the renaming of a file, processing it if it now ends with .mp3.
    
        Args:
            event: The filesystem event object.
        """
        if not event.is_directory:
            file_path = event.dest_path
            if file_path.endswith('.mp3'):
                logging.info(f"New mp3 file: {file_path}")
                frequency = self.extract_frequency(file_path)
                if frequency and frequency in self.room_ids:
                    room_id = self.room_ids[frequency]
                    # Add the task to the queue instead of creating it directly
                    self.upload_queue.put_nowait((file_path, room_id))
                else:
                    logging.warning(f"No room found for frequency in mp3 file: {file_path}")

    def extract_frequency(self, file_path: str) -> int:
        """
        Extract frequency from the filename (placeholder implementation).

        Args:
            file_path (str): Path to the file.

        Returns:
            int: Extracted frequency or None if not found.
        """
        import re
        filename = os.path.basename(file_path)
        match = re.search(r'_(\d+)\.mp3$', filename)
        return int(match.group(1)) if match else None

def extract_channels_content(content):
    # Find the start of the channels section
    channels_start = content.find("channels:")
    if channels_start == -1:
        logging.warning("No 'channels:' section found in config.")
        return None
    
    # Find the opening '(' after 'channels:'
    paren_start = content.find("(", channels_start)
    if paren_start == -1:
        logging.warning("No opening '(' found after 'channels:'.")
        return None
    
    # Find the matching ');' considering nested parentheses
    count = 0
    for i in range(paren_start, len(content)):
        if content[i] == '(':
            count += 1
        elif content[i] == ')':
            count -= 1
            if count == 0:
                # Found the matching ')', check for ';'
                if i + 1 < len(content) and content[i + 1] == ';':
                    return content[paren_start + 1:i].strip()
                else:
                    logging.warning("No ';' found after closing ')'.") 
                    return None
    logging.warning("No matching ');' found for 'channels: ('.") 
    return None

def parse_channels(config_path):
    """
    Parse the channels from rtl_airband.conf and return a list of frequencies in Hz.
    
    Args:
        config_path (str): Path to the configuration file.
    
    Returns:
        list: List of frequencies in Hertz (integers).
    """
    # Get the environment variable, defaulting to 'true' (skip disabled channels)
    skip_disabled = os.getenv('SKIP_DISABLED_CHANNELS', 'true').lower() == 'true'
    
    frequencies = []
    non_disabled_count = 0
    
    try:
        with open(config_path, 'r') as f:
            content = f.read()
    
        # Extract the channels content
        channels_content = extract_channels_content(content)
        if channels_content is None:
            return []
        
        # Log the extracted content for debugging
        logging.debug(f"Channels content extracted: {channels_content}")
        
        # Find all channel blocks within '{}'
        channel_blocks = re.findall(r'\{.*?\}', channels_content, re.DOTALL)
        logging.info(f"Found {len(channel_blocks)} channel blocks.")
        
        # Iterate through each channel block
        for block in channel_blocks:
            # Check if the channel is disabled
            is_disabled = 'disable = true;' in block
            
            # Increment counter if the channel is not disabled
            if not is_disabled:
                non_disabled_count += 1
            
            # Include the channel's frequency if we're not skipping disabled channels,
            # or if the channel is not disabled
            if not skip_disabled or not is_disabled:
                freq_match = re.search(r'freq\s*=\s*(.*?);', block)
                if freq_match:
                    freq_str = freq_match.group(1).strip()
                    try:
                        freq_hz = parse_frequency(freq_str)
                        frequencies.append(freq_hz)
                    except ValueError as e:
                        logging.warning(f"Failed to parse frequency '{freq_str}': {e}")

        # Log the number of non-disabled channels
        logging.info(f"Number of non-disabled channels: {non_disabled_count}")
    
    except FileNotFoundError:
        logging.error(f"Config file not found: {config_path}")
        return []
    except Exception as e:
        logging.error(f"Error reading config file: {e}")
        return []
    
    return frequencies

def parse_frequency(value_str):
    """
    Parse a frequency string into an integer in Hertz.
    
    Args:
        value_str (str): The frequency value (e.g., '121500000', '121.5', '"121.5M"').
    
    Returns:
        int: Frequency in Hertz.
    
    Raises:
        ValueError: If the frequency format is invalid.
    """
    value_str = value_str.strip()
    
    # Check if the value is a quoted string
    if value_str.startswith('"') and value_str.endswith('"'):
        # Remove quotes
        value_str = value_str[1:-1]
        # Match numeric part and optional multiplier (e.g., '121.5M', '121500k', '121500000')
        match = re.match(r'(\d+\.?\d*)([kKmMgG]?)$', value_str)
        if not match:
            raise ValueError(f"Invalid frequency string: {value_str}")
        
        num_part = float(match.group(1))  # Convert numeric part to float
        multiplier = match.group(2).lower() if match.group(2) else ''
        
        # Apply multiplier
        if multiplier == 'k':
            return int(num_part * 1000)
        elif multiplier == 'm':
            return int(num_part * 1000000)
        elif multiplier == 'g':
            return int(num_part * 1000000000)
        else:
            return int(num_part)  # No multiplier, assume Hz
    else:
        # It's a number (not quoted)
        if '.' in value_str:
            # Float in MHz
            return int(float(value_str) * 1000000)
        else:
            # Integer in Hz
            return int(value_str)

async def get_or_create_room(client, frequency, domain):
    """
    Get or create a Matrix room for the given frequency.
    Returns the room ID.
    """
    # Convert frequency (in Hz) to a readable string (e.g., "145.500MHz")
    freq_str = f"{frequency / 1000000:.3f}MHz"
    full_alias = f"#{freq_str}:{domain}"
    
    # Check if the room alias exists
    response = await client.room_resolve_alias(full_alias)
    if hasattr(response, 'room_id') and response.room_id:
        return response.room_id
    
    # Room doesn’t exist, create it
    create_response = await client.room_create(
        alias=freq_str,  # Local part of the alias (e.g., "145.500MHz")
        name=f"Recordings for {freq_str}",
        topic=f"Audio recordings for frequency {freq_str}",
        visibility=RoomVisibility.public  # Optional: makes the room joinable without invite
    )
    if isinstance(create_response, RoomCreateError):
        raise Exception(f"Failed to create room for {freq_str}: {create_response.message}")
    return create_response.room_id

async def main():
    """Main function to set up and run the uploader."""
    logging.basicConfig(level=logging.INFO)
    
    # Initialize Matrix client
    client = AsyncClient(
        os.getenv("SYNAPSE_URL"),
        f"@{os.getenv('BOT_USER')}:{os.getenv('MATRIX_DOMAIN')}"
    )
    await client.login(os.getenv("BOT_PASSWORD"))
    
    # Parse frequencies from config
    config_path = "/etc/rtl_airband.conf"
    frequencies = parse_channels(config_path)
    if not frequencies:
        logging.error("No frequencies found in config file")
        await client.close()
        return
    
    # Map frequencies to room IDs
    room_ids = {}
    domain = os.getenv("MATRIX_DOMAIN")
    for frequency in frequencies:
        # Assume get_or_create_room is defined elsewhere
        room_id = await get_or_create_room(client, frequency, domain)
        room_ids[frequency] = room_id
        logging.info(f"Mapped frequency {frequency} Hz to room {room_id}")
    
    # Set up the recordings directory observer
    recordings_path = "/recordings"
    if not os.path.exists(recordings_path):
        os.makedirs(recordings_path)
        logging.info(f"Created directory {recordings_path}")
    
    # Create an asyncio queue for upload tasks
    upload_queue = asyncio.Queue()

    # Initialize the UploadHandler with the queue
    handler = UploadHandler(client, room_ids, upload_queue)

    # Set up the watchdog observer
    observer = Observer()
    observer.schedule(handler, recordings_path, recursive=False)
    observer.start()
    logging.info(f"Started observer for {recordings_path}")
    
    # Keep the script running
    try:
        # Main loop to process upload tasks from the queue
        while True:
            file_path, room_id = await upload_queue.get()  # Wait for a task
            await handler.upload_file(file_path, room_id)  # Run the upload
            upload_queue.task_done()  # Mark the task as complete
    except asyncio.CancelledError:
        observer.stop()
    finally:
        observer.join()
        await client.close()  # Clean up your client

if __name__ == "__main__":
    asyncio.run(main())