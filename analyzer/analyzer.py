import os
import time
import logging
from itertools import cycle
from groq import Groq
from ratelimit import limits, sleep_and_retry
from .config import tokens
from typing import List
from requests.exceptions import HTTPError, ConnectionError, Timeout
import functools
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("code_analyzer.log"),
        logging.StreamHandler()
    ]
)

CALLS = 5
PERIOD = 1

def retry(exceptions, tries=3, delay=1, backoff=2):
    """
    Decorator that retries a function if specified exceptions occur.

    :param exceptions: Exception or tuple of exceptions to catch and retry upon.
    :param tries: Maximum number of attempts before giving up.
    :param delay: Initial delay between retries in seconds.
    :param backoff: Multiplier applied to delay between retries.
    :return: Decorated function with retry logic.
    """
    def decorator_retry(func):
        @functools.wraps(func)
        def wrapper_retry(*args, **kwargs):
            _tries, _delay = tries, delay
            while _tries > 1:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    logging.warning(f"{e}, retrying in {_delay} seconds...")
                    time.sleep(_delay)
                    _tries -= 1
                    _delay *= backoff
            return func(*args, **kwargs)
        return wrapper_retry
    return decorator_retry

class CodeAnalyzer:
    """
    Analyzes code files for vulnerabilities using the GROQ API.
    """

    SUPPORTED_EXTENSIONS = (".py", ".js", ".java", ".cpp", ".c", ".cs", ".ts", ".php")

    def __init__(self, directory: str, max_retries: int = 5, timeout: float = 20.0):
        """
        Initializes the CodeAnalyzer.

        :param directory: The root directory containing code files to analyze.
        :param max_retries: Maximum number of retries for API requests.
        :param timeout: Timeout for API requests in seconds.
        """
        self.directory = directory
        self.token_cycle = cycle(tokens)
        self.client = Groq(max_retries=max_retries, timeout=timeout)

    def get_next_token(self) -> str:
        """
        Retrieves the next token from the token cycle.

        :return: Next API token as a string.
        """
        return next(self.token_cycle)

    @sleep_and_retry
    @limits(calls=CALLS, period=PERIOD)
    @retry((HTTPError, ConnectionError, Timeout), tries=3)
    def process_code(self, file_path: str, model_token: str, content: str) -> str:
        """
        Processes a chunk of code by sending it to the GROQ API for analysis.

        :param file_path: Path to the code file being processed.
        :param model_token: The model token for the GROQ API.
        :param content: The code content to be analyzed.
        :return: The response from the API as a string.
        """
        try:
            logging.info(f"Processing {file_path} with token {model_token}")
            chat_completion = self.client.chat.completions.create(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a security code analyzer specialized in static code analysis. Analyze the provided code snippet for vulnerabilities, "
                            "secrets, and code quality issues. For each issue found, provide:\n"
                            "- Severity level (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `INFO`)\n"
                            "- A brief description of the issue\n"
                            "- The line number where the issue occurs (if available)\n"
                            "If no issues are found, respond with 'SEVERITY: INFO - No significant vulnerabilities detected.'\n"
                            "Provide your response in the following JSON format:\n"
                            "{\n"
                            "  \"issues\": [\n"
                            "    {\n"
                            "      \"severity\": \"<SEVERITY_LEVEL>\",\n"
                            "      \"description\": \"<ISSUE_DESCRIPTION>\",\n"
                            "      \"line\": <LINE_NUMBER>\n"
                            "    },\n"
                            "    ...\n"
                            "  ]\n"
                            "}\n"
                            "Do not include any additional text outside of the JSON format."
                        )
                    },
                    {"role": "user", "content": content},
                ],
                model=model_token,
                temperature=0.1,
                max_tokens=512,
                top_p=1,
                stream=False
            )
            return chat_completion.choices[0].message.content
        except (HTTPError, ConnectionError, Timeout) as e:
            logging.error(f"Network error processing {file_path}: {e}")
            return f"Network error: {e}"
        except Exception as e:
            logging.exception(f"Unhandled exception processing {file_path}")
            return f"Error: {e}"

    def split_content(self, content: str, max_length: int = 5000) -> List[str]:
        """
        Splits the content into chunks of a specified maximum length.

        :param content: The content to split.
        :param max_length: The maximum length of each chunk.
        :return: A list of content chunks.
        """
        return [content[i:i + max_length] for i in range(0, len(content), max_length)]

    def is_supported_file(self, filename: str) -> bool:
        """
        Checks if the file has a supported extension.

        :param filename: Name of the file.
        :return: True if supported, False otherwise.
        """
        return filename.endswith(self.SUPPORTED_EXTENSIONS)

    def read_file(self, file_path: str) -> str:
        """
        Reads the content of a file with proper encoding handling.

        :param file_path: The path to the file to read.
        :return: The content of the file as a string.
        :raises Exception: If the file cannot be read.
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                return file.read()
        except UnicodeDecodeError:
            with open(file_path, 'r', encoding='latin-1') as file:
                return file.read()

    def analyze(self, html_report) -> None:
        """
        Analyzes code files in the directory and updates the HTML report.

        :param html_report: An instance of HTMLReport to collect analysis results.
        """
        for root, _, files in os.walk(self.directory):
            for filename in files:
                if not self.is_supported_file(filename):
                    continue

                file_path = os.path.join(root, filename)
                current_token = self.get_next_token()
                logging.info(f"Analyzing file: {file_path}")

                try:
                    content = self.read_file(file_path)
                except Exception as e:
                    logging.error(f"Failed to read {file_path}: {e}")
                    continue

                contents = self.split_content(content) if len(content) > 5000 else [content]
                summaries = self.collect_issues(file_path, current_token, contents)
                html_report.add_file_summary(file_path, summaries)

    def collect_issues(self, file_path: str, token: str, contents: List[str]) -> List:
        """
        Processes content chunks and collects issues.

        :param file_path: Path to the file being analyzed.
        :param token: Token for processing.
        :param contents: List of content chunks.
        :return: List of issues found.
        """
        summaries = []
        for content_chunk in contents:
            result = self.process_code(file_path, token, content_chunk)
            issues = self.parse_issues(result, file_path)
            summaries.extend(issues)
        return summaries

    def parse_issues(self, result: str, file_path: str) -> List:
        """
        Parses the analysis result and extracts issues.

        :param result: The analysis result as a string.
        :param file_path: Path to the file being analyzed.
        :return: List of issues extracted from the result.
        """
        try:
            issues = json.loads(result).get('issues', [])
            if not issues:
                logging.info(f"{file_path}: No significant vulnerabilities detected.")
            return issues
        except json.JSONDecodeError:
            logging.error(f"Failed to parse JSON response for {file_path}")
            return []
