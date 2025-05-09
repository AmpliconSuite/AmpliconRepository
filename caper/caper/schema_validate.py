#!/usr/bin/env python3

import argparse
import json
import os
import sys
from jsonschema import validate, ValidationError
from typing import Any, Dict, List, Optional, Tuple

# Import the database utilities from utils module
# Adjust the import based on your project structure
try:
    # When imported as a module within the same package
    from .utils import get_db_handle, mongo_client
except ImportError:
    # When run as a standalone script
    from utils import get_db_handle, mongo_client


def generate_schema(data: Any) -> Dict[str, Any]:
    """
    Generates a basic JSON schema from a Python object (dict, list, str, int, etc.).

    Handles nested objects and lists. For lists, the 'items' schema is based
    on the schema of the first element found. If the list is empty, 'items'
    will be an empty schema ({}), allowing any type.

    Args:
        data: The Python object representing JSON data. Can be a dict, list,
              str, int, float, bool, or None.

    Returns:
        A dictionary representing the inferred JSON schema.

    Raises:
        TypeError: If an unsupported data type is encountered.
    """
    schema: Dict[str, Any] = {}  # Initialize the schema dictionary

    if isinstance(data, dict):
        # Handle dictionaries (JSON objects)
        schema["type"] = "object"
        properties: Dict[str, Any] = {}
        required_keys: List[str] = []
        if data:  # Check if the dictionary is not empty
            for key, value in data.items():
                # Recursively generate schema for each value
                properties[key] = generate_schema(value)
                # Assume all keys found are required for this basic version
                required_keys.append(key)
            schema["properties"] = properties
            if required_keys:
                schema["required"] = sorted(required_keys)  # Sort for consistency
        else:
            # Empty object
            schema["properties"] = {}
            # schema["required"] = [] # Optional: explicitly add empty required list

    elif isinstance(data, list):
        # Handle lists (JSON arrays)
        schema["type"] = "array"
        if data:  # Check if the list is not empty
            # --- Basic approach: Infer schema from the first item ---
            first_item_schema = generate_schema(data[0])
            schema["items"] = first_item_schema
        else:
            # Empty list: Define 'items' as an empty schema, allowing any type.
            schema["items"] = {}

    elif isinstance(data, str):
        schema["type"] = "string"
    elif isinstance(data, bool):
        schema["type"] = "boolean"
    elif isinstance(data, int):
        # JSON Schema distinguishes between integer and number
        schema["type"] = "integer"
    elif isinstance(data, float):
        schema["type"] = "number"
    elif data is None:
        schema["type"] = "null"
    else:
        # Handle types that don't directly map to JSON Schema standard types
        raise TypeError(f"Unsupported data type for JSON schema generation: {type(data)}")

    return schema


def parse_arguments():
    """Parses command-line arguments."""
    # Get the database name from environment variable as default
    default_db_name = os.environ.get("DB_NAME", "caper")

    parser = argparse.ArgumentParser(
        description="Validate documents in a MongoDB collection against a JSON schema."
    )
    parser.add_argument(
        "--db", "-d",
        dest="db_host",
        default=None,
        help="MongoDB connection string (if not provided, will use the one from utils)"
    )
    parser.add_argument(
        "--db-name",
        dest="db_name",
        default=default_db_name,
        help=f"Name of the database to use (default: {default_db_name})"
    )
    parser.add_argument(
        "--collection", "-c",
        dest="collection_name",
        default="projects",
        help="Name of the MongoDB collection to validate (default: projects)"
    )
    parser.add_argument(
        "--schema", "-s",
        dest="schema_path",
        default="schema/schema.json",
        help="Path to the JSON schema file (default: schema/schema.json)"
    )
    return parser.parse_args()


def load_schema(schema_path):
    """Loads the JSON schema from the specified file path."""
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        print(f"Successfully loaded schema from '{schema_path}'")
        return schema
    except FileNotFoundError:
        print(f"Error: Schema file not found at '{schema_path}'", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file '{schema_path}': {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading schema file '{schema_path}': {e}", file=sys.stderr)
        sys.exit(1)


def validate_collection(
        schema: Dict[str, Any],
        collection_name: str = "projects",
        db_handle=None,
        client=None,
        close_connection: bool = False
) -> Tuple[int, int, int]:
    """
    Validates documents in a collection against a schema.

    Args:
        schema: The JSON schema to validate against
        collection_name: Name of the MongoDB collection to validate
        db_handle: Optional database handle (if None, uses the one from utils)
        client: Optional MongoDB client (if None, uses the one from utils)
        close_connection: Whether to close the client connection when done

    Returns:
        A tuple of (total documents, invalid documents, error count)
    """
    doc_count = 0
    invalid_count = 0
    error_count = 0

    try:
        collection = db_handle[collection_name]
        print(f"Using database: '{db_handle.name}', collection: '{collection_name}'")

        # Fetch and Validate Documents
        print(f"\n--- Validating documents in '{collection_name}' ---")

        cursor = collection.find()
        for doc in cursor:
            doc_count += 1

            # Skip deleted projects
            deleted = doc.get('delete', False)
            if deleted: continue

            # --- Identify the document ---
            # Use 'project_name' or 'name' field if available, otherwise use _id
            doc_id_str = str(doc.get('_id', 'UNKNOWN_ID'))
            identifier = doc.get('project_name', doc.get('name', f"Document (ID: {doc_id_str})"))
            creator = doc.get('creator', 'Unknown User')

            # --- Prepare document for validation ---
            # Create a copy and remove MongoDB's _id field
            doc_to_validate = doc.copy()
            if '_id' in doc_to_validate:
                del doc_to_validate['_id']

            # --- Perform Validation ---
            try:
                validate(instance=doc_to_validate, schema=schema)
                print(f"- '{identifier}' ({creator}): VALID")
            except ValidationError as e:
                invalid_count += 1
                print(f"- '{identifier}' ({creator}): NOT VALID")
                # Provide specific error details
                print(f"  Reason: {e.message}")  # Primary error message
                if e.path:
                    # e.path is a deque, convert to list for cleaner printing
                    print(f"  Path: /{' / '.join(map(str, e.path))}")
                if e.validator:
                    print(f"  Schema Keyword: '{e.validator}'")
            except Exception as e:  # Catch unexpected errors during validation for *this* doc
                error_count += 1
                print(f"- '{identifier}': ERROR during validation")
                print(f"  Unexpected validation error: {type(e).__name__} - {e}")

        # Check if any documents were processed
        if doc_count == 0:
            print(f"\nWarning: No documents found in collection '{collection_name}'.")

        # Print Summary
        print("\n--- Validation Summary ---")
        print(f"Total documents processed: {doc_count}")
        print(f"Valid documents: {doc_count - invalid_count - error_count}")
        print(f"Invalid documents: {invalid_count}")
        if error_count > 0:
            print(f"Errors during processing: {error_count}")

        return doc_count, invalid_count, error_count

    except Exception as e:
        print(f"Error during validation: {e}", file=sys.stderr)
        return doc_count, invalid_count, error_count + 1
    finally:
        # Close the connection if requested and if we have a client
        if close_connection and client is not None:
            client.close()
            print("MongoDB connection closed.")


def run_validation(
        db_host: Optional[str] = None,
        db_name: Optional[str] = None,
        collection_name: str = "projects",
        schema_path: str = "schema/schema.json"
) -> str:
    """
    Run the validation process and return the output as a string.
    This function can be called from other Python modules.

    Args:
        db_host: MongoDB connection string (if None, uses the one from utils)
        db_name: Name of the database to use (if None, uses environment variable or default)
        collection_name: Name of the MongoDB collection to validate
        schema_path: Path to the JSON schema file

    Returns:
        A string containing the validation report
    """
    import io
    import sys

    # Capture stdout
    original_stdout = sys.stdout
    captured_output = io.StringIO()
    sys.stdout = captured_output

    try:
        # Use default db_name if not provided
        if db_name is None:
            db_name = os.environ.get("DB_NAME", "caper")

        # Load schema
        schema = load_schema(schema_path)

        # Set up database connection if needed
        client = None
        db_handle = None

        if db_host:
            # If a database host is provided, create a new connection
            db_handle, client = get_db_handle(db_name, db_host)
            print(f"Connected to MongoDB at '{db_host}', database '{db_name}'")
        else:
            # Use the existing connection from utils, but with the specified db_name
            db_handle = mongo_client[db_name]
            client = mongo_client
            print(f"Using existing MongoDB connection from utils, database '{db_name}'")

        # Run validation
        validate_collection(
            schema=schema,
            collection_name=collection_name,
            db_handle=db_handle,
            client=client,
            close_connection=bool(db_host)  # Only close if we created a new connection
        )

        # Return the captured output
        return captured_output.getvalue()

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return captured_output.getvalue()

    finally:
        # Restore stdout
        sys.stdout = original_stdout


def main():
    """Main function for command-line usage."""
    args = parse_arguments()

    try:
        # Load schema
        schema = load_schema(args.schema_path)

        # Set up database connection if needed

        if args.db_host:
            # If a database host is provided, create a new connection
            # Use the db_name from arguments
            db_handle, client = get_db_handle(args.db_name, args.db_host)
            print(f"Connected to MongoDB at '{args.db_host}', database '{args.db_name}'")
        else:
            # Use the existing connection from utils, but with the specified db_name
            db_handle = mongo_client[args.db_name]
            client = mongo_client
            print(f"Using existing MongoDB connection from utils, database '{args.db_name}'")

        # Run validation
        validate_collection(
            schema=schema,
            collection_name=args.collection_name,
            db_handle=db_handle,
            client=client,
            close_connection=bool(args.db_host)  # Only close if we created a new connection
        )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()