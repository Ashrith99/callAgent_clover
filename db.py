# Import OS module for environment variable access
import os

# Load environment variables from a .env file
from dotenv import load_dotenv

# MongoDB client and error classes
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Typing helper for optional return values
from typing import Optional, List, Dict, Any
import logging

# Import datetime for timestamps
from datetime import datetime

# Import Clover integration
try:
    from clover import get_clover_client
    CLOVER_ENABLED = True
except Exception as e:
    CLOVER_ENABLED = False
    logging.getLogger("realtime_restaurant_agent").warning(f"Clover integration disabled: {e}")

# ---------- Load .env file and initialize MongoDB URI ----------

# Load environment variables from the .env file into the environment
load_dotenv()

# Retrieve MongoDB URI from environment variables
MONGO_URI = os.getenv("MONGO_URI")

# ---------- MongoDB Connection Setup ----------

try:
    # Initialize MongoDB client with the URI
    if not MONGO_URI:
        raise ValueError("MONGO_URI environment variable not set.")
    
    # Disable MongoDB logs by setting logging level
    import logging
    logging.getLogger("pymongo").setLevel(logging.WARNING)
    logging.getLogger("pymongo.connection").setLevel(logging.WARNING)
    logging.getLogger("pymongo.pool").setLevel(logging.WARNING)
    logging.getLogger("pymongo.serverSelection").setLevel(logging.WARNING)
    
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)

    # Access the 'restaurant' database
    db = client["restaurant"]

    # Access the 'orders' collection within the 'restaurant' database
    orders_collection = db["orders"]

except (PyMongoError, ValueError) as e:
    # Re-raise, but also log for visibility
    logging.getLogger("realtime_restaurant_agent").error(f"Mongo init failed: {e}")
    raise

# ---------- Order Database Driver Class ----------

class DatabaseDriver:
    def __init__(self):
        # Initialize the collection reference to use in other methods
        self.collection = orders_collection
        self.log = logging.getLogger("realtime_restaurant_agent")
        
        # Create indexes for faster queries (50-100ms → 10-20ms)
        try:
            self.collection.create_index("phone", background=True)
            self.collection.create_index("created_at", background=True)
            self.log.info("Database indexes created successfully")
        except Exception as e:
            self.log.warning(f"Index creation warning: {e}")

    # Create a new order in the MongoDB collection
    def create_order(self, phone: str, items: List[Dict[str, Any]], name: str = None, address: str = None, caller_phone: str = None) -> Optional[dict]:
        self.log.info(f"Database: Received phone parameter: {phone}")
        self.log.info(f"Database: Phone parameter type: {type(phone)}")
        self.log.info(f"Database: Phone parameter is None: {phone is None}")
        self.log.info(f"Database: Phone parameter == 'unknown': {phone == 'unknown'}")
        
        # NEVER allow "unknown" phone numbers - always use a fallback
        if not phone or phone == "unknown":
            import time
            phone = f"call_{int(time.time())}"
            self.log.info(f"Database: Replaced 'unknown' with fallback phone: {phone}")
        
        self.log.info(f"Database: Final phone number for order: {phone}")
        
        order = {
            "phone": phone,
            "items": items,
            "status": "confirmed",
            "created_at": datetime.now().isoformat(),
            "order_type": "phone_only"  # Indicates this is a phone-only order
        }
        
        # Add optional fields if provided
        if name:
            order["name"] = name
        if address:
            order["address"] = address
        
        # Add caller phone number if available
        if caller_phone:
            order["caller_phone"] = caller_phone
            order["phone_source"] = "extracted_from_call"
        else:
            order["phone_source"] = "provided_by_customer"
        
        try:
            self.log.info(f"Database: Inserting order with phone: {order.get('phone')}")
            self.log.info(f"Database: Full order document: {order}")
            # Insert the order document into the MongoDB collection
            result = self.collection.insert_one(order)
            self.log.info(f"Database: Insert result: {result.inserted_id}")
            
            # Add MongoDB ID to order for reference
            order["_id"] = str(result.inserted_id)
            
            return order
        except PyMongoError as e:
            self.log.error(f"Database: Insert failed: {e}")
            return None
    
    # Create order and sync to Clover POS (async version)
    async def create_order_with_clover(self, phone: str, items: List[Dict[str, Any]], name: str = None, address: str = None, caller_phone: str = None) -> Optional[dict]:
        """
        Create order in MongoDB and sync to Clover POS.
        
        This is an async wrapper that:
        1. Saves order to MongoDB (your database)
        2. Sends order to Clover POS (restaurant system)
        
        Args:
            phone: Customer phone number
            items: List of order items
            name: Customer name (optional)
            address: Delivery address (optional)
            caller_phone: Extracted caller phone (optional)
        
        Returns:
            Order document if successful, None otherwise
        """
        # Step 1: Save to MongoDB (always do this first)
        order = self.create_order(phone, items, name, address, caller_phone)
        
        if not order:
            self.log.error("Failed to save order to MongoDB")
            return None
        
        # Step 2: Sync to Clover POS (don't fail if this doesn't work)
        if CLOVER_ENABLED:
            try:
                clover_client = get_clover_client()
                clover_order_id = await clover_client.create_order(
                    phone=phone,
                    items=items,
                    name=name,
                    address=address
                )
                
                if clover_order_id:
                    self.log.info(f"✅ Order synced to Clover POS: {clover_order_id}")
                    # Update MongoDB with Clover order ID
                    try:
                        from bson.objectid import ObjectId
                        self.collection.update_one(
                            {"_id": ObjectId(order["_id"])},
                            {"$set": {"clover_order_id": clover_order_id}}
                        )
                        order["clover_order_id"] = clover_order_id
                    except Exception as e:
                        self.log.warning(f"Could not update Clover order ID in MongoDB: {e}")
                else:
                    self.log.warning("⚠️ Failed to sync order to Clover (order saved in MongoDB)")
            except Exception as e:
                self.log.error(f"⚠️ Clover sync error: {e} (order saved in MongoDB)")
        else:
            self.log.info("Clover integration disabled - order saved to MongoDB only")
        
        return order

    # Retrieve an order document by phone number
    def get_order_by_phone(self, phone: str) -> Optional[dict]:
        try:
            # Search for an order with the matching phone number, get the most recent one
            order = self.collection.find_one({"phone": phone}, sort=[("_id", -1)])
            return order
        except PyMongoError:
            return None
