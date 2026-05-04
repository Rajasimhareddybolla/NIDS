import json
import os
from pymongo import MongoClient

def seed():
    # Connect to MongoDB using env var or default to localhost
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    client = MongoClient(MONGO_URI)
    db = client["nids_analysis"]
    
    col_benchmarks = db["benchmarks"]
    col_models = db["models"]

    # Clear existing data for idempotency
    col_benchmarks.delete_many({})
    col_models.delete_many({})

    # Seed Head-to-Head Results
    try:
        with open("logs/head_to_head_results.json", "r") as f:
            data = json.load(f)
            col_benchmarks.insert_one(data)
            print("Successfully inserted head_to_head_results.json into MongoDB!")
    except Exception as e:
        print(f"Error loading head_to_head_results.json: {e}")
    
    # Seed Model Evaluation Results
    try:
        with open("logs/high_recall_xgb_20260430T053442Z.json", "r") as f:
            data = json.load(f)
            col_models.insert_one(data)
            print("Successfully inserted high_recall_xgb_20260430T053442Z.json into MongoDB!")
    except Exception as e:
        print(f"Error loading high_recall_xgb_20260430T053442Z.json: {e}")

if __name__ == "__main__":
    print(f"Connecting to MongoDB at {os.getenv('MONGO_URI', 'mongodb://localhost:27017/')}")
    seed()
