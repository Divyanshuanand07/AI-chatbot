"""
Reference pools for seeding — real-shaped Indian used-car retail data.

Kept separate from the scenario logic so the catalogue stays about
*operational situations* and this file stays about *plausible values*.

Realism is not cosmetic here. Registration plates follow real RTO series,
prices match the actual used-car band for each model, and hubs match their
city. If the seeded data looked synthetic, you could not tell whether a bad
assistant answer came from a bad prompt or from nonsense inputs.
"""

from __future__ import annotations

# (city, state, hub name, plate series)
LOCATIONS: list[tuple[str, str, str, str]] = [
    ("Gurugram", "Haryana", "Gurugram Sector 48 Hub", "HR26"),
    ("Noida", "Uttar Pradesh", "Noida Sector 63 Hub", "UP16"),
    ("New Delhi", "Delhi", "Delhi Okhla Hub", "DL8C"),
    ("Bengaluru", "Karnataka", "Bengaluru Whitefield Hub", "KA03"),
    ("Hyderabad", "Telangana", "Hyderabad Kondapur Hub", "TS08"),
    ("Pune", "Maharashtra", "Pune Wakad Hub", "MH12"),
    ("Mumbai", "Maharashtra", "Mumbai Andheri Hub", "MH02"),
    ("Chennai", "Tamil Nadu", "Chennai OMR Hub", "TN10"),
    ("Ahmedabad", "Gujarat", "Ahmedabad SG Highway Hub", "GJ01"),
    ("Kolkata", "West Bengal", "Kolkata Salt Lake Hub", "WB02"),
    ("Jaipur", "Rajasthan", "Jaipur Malviya Nagar Hub", "RJ14"),
    ("Lucknow", "Uttar Pradesh", "Lucknow Gomti Nagar Hub", "UP32"),
]

# (make, model, variant, fuel, transmission, price_low, price_high)
VEHICLE_CATALOGUE: list[tuple[str, str, str, str, str, int, int]] = [
    ("Maruti Suzuki", "Swift", "VXI", "Petrol", "Manual", 385000, 610000),
    ("Maruti Suzuki", "Baleno", "Zeta", "Petrol", "Manual", 520000, 780000),
    ("Maruti Suzuki", "Wagon R", "LXI", "Petrol", "Manual", 310000, 495000),
    ("Maruti Suzuki", "Dzire", "VXI", "Petrol", "Automatic", 480000, 720000),
    ("Maruti Suzuki", "Vitara Brezza", "ZXI", "Petrol", "Manual", 680000, 960000),
    ("Maruti Suzuki", "Ertiga", "VXI", "Petrol", "Manual", 620000, 920000),
    ("Hyundai", "i20", "Sportz", "Petrol", "Manual", 490000, 760000),
    ("Hyundai", "Creta", "SX", "Diesel", "Automatic", 890000, 1450000),
    ("Hyundai", "Grand i10 Nios", "Magna", "Petrol", "Manual", 400000, 615000),
    ("Hyundai", "Venue", "S Plus", "Petrol", "Manual", 640000, 930000),
    ("Hyundai", "Verna", "SX", "Petrol", "Automatic", 720000, 1120000),
    ("Tata", "Nexon", "XZ Plus", "Petrol", "Manual", 660000, 1010000),
    ("Tata", "Altroz", "XZ", "Petrol", "Manual", 520000, 780000),
    ("Tata", "Tiago", "XZ", "Petrol", "Manual", 380000, 590000),
    ("Tata", "Harrier", "XZ", "Diesel", "Manual", 1080000, 1650000),
    ("Honda", "City", "VX", "Petrol", "Automatic", 620000, 1150000),
    ("Honda", "Amaze", "S", "Petrol", "Manual", 430000, 680000),
    ("Honda", "Jazz", "VX", "Petrol", "Manual", 460000, 700000),
    ("Mahindra", "XUV300", "W6", "Diesel", "Manual", 680000, 1010000),
    ("Mahindra", "Scorpio", "S11", "Diesel", "Manual", 920000, 1450000),
    ("Mahindra", "Thar", "LX", "Diesel", "Automatic", 1150000, 1720000),
    ("Mahindra", "XUV700", "AX5", "Diesel", "Automatic", 1420000, 2050000),
    ("Kia", "Seltos", "HTK Plus", "Petrol", "Manual", 920000, 1430000),
    ("Kia", "Sonet", "HTX", "Diesel", "Manual", 740000, 1120000),
    ("Toyota", "Innova Crysta", "GX", "Diesel", "Manual", 1350000, 2150000),
    ("Toyota", "Glanza", "G", "Petrol", "Manual", 520000, 760000),
    ("Renault", "Kwid", "RXT", "Petrol", "Manual", 260000, 430000),
    ("Renault", "Triber", "RXZ", "Petrol", "Manual", 420000, 660000),
    ("Volkswagen", "Polo", "Highline Plus", "Petrol", "Manual", 480000, 760000),
    ("Ford", "EcoSport", "Titanium", "Petrol", "Manual", 480000, 740000),
    ("Nissan", "Magnite", "XV", "Petrol", "Manual", 520000, 760000),
    ("MG", "Hector", "Sharp", "Diesel", "Manual", 1080000, 1620000),
]

COLOURS = [
    "White", "Silver", "Grey", "Red", "Blue", "Black",
    "Bronze", "Pearl White", "Metallic Grey", "Fiery Red",
]

FIRST_NAMES = [
    "Aarav", "Aditya", "Akash", "Ananya", "Anjali", "Arjun", "Bhavna",
    "Chetan", "Deepak", "Divya", "Farhan", "Gaurav", "Harpreet", "Ishaan",
    "Jyoti", "Kavya", "Kiran", "Lakshmi", "Manish", "Meera", "Nikhil",
    "Nisha", "Pooja", "Pranav", "Priya", "Rahul", "Rajesh", "Rekha",
    "Rohit", "Sahil", "Sanjay", "Shreya", "Siddharth", "Sneha", "Sunil",
    "Swati", "Tarun", "Neha", "Varun", "Vikram", "Vishal", "Yash",
    "Zoya", "Imran", "Ritu", "Abhishek", "Pallavi", "Suresh",
]

LAST_NAMES = [
    "Sharma", "Verma", "Gupta", "Singh", "Patel", "Reddy", "Naidu",
    "Iyer", "Nair", "Menon", "Rao", "Desai", "Joshi", "Kulkarni",
    "Deshpande", "Chatterjee", "Banerjee", "Mukherjee", "Bose", "Das",
    "Khan", "Ansari", "Kaur", "Chopra", "Malhotra", "Kapoor", "Bhatia",
    "Agarwal", "Jain", "Shah", "Mehta", "Pandey", "Mishra", "Tiwari",
    "Yadav", "Thakur", "Pillai", "Krishnan",
]

AGENTS = [
    "Rohan Mehta", "Priyanka Nair", "Sameer Qureshi", "Neelam Bhatt",
    "Aditya Kulkarni", "Fatima Shaikh", "Vivek Ranjan", "Shalini Rao",
    "Karan Ahluwalia", "Deepti Menon", "Nitin Saxena", "Ayesha Khan",
]

FINANCE_PARTNERS = [
    "Cars24 Financial Services",
    "HDFC Bank",
    "ICICI Bank",
    "IDFC First Bank",
    "Kotak Mahindra Prime",
    "Bajaj Finance",
    "Axis Bank",
]

GATEWAYS = ["Razorpay", "PayU", "Cashfree", "Paytm Business"]

LOGISTICS_PARTNERS = [
    "Cars24 Logistics",
    "Delhivery Heavy",
    "BlackBuck",
    "Rivigo Auto Carrier",
]

DELIVERY_SLOTS = [
    "10:00 - 12:00",
    "12:00 - 14:00",
    "14:00 - 16:00",
    "16:00 - 18:00",
    "18:00 - 20:00",
]

CHANNEL_WEIGHTS = [
    ("APP", 40),
    ("WEB", 25),
    ("HUB_WALKIN", 18),
    ("TELESALES", 12),
    ("PARTNER", 5),
]

#: Human-readable descriptions for derived timeline events, keyed by event
#: type. Keeping the wording here (rather than inline) means the timeline
#: reads consistently no matter which scenario produced the event.
EVENT_TEMPLATES = {
    "ORDER_CREATED": "Order created via {channel} for {vehicle}.",
    "STATUS_CHANGED": "Status changed from {from_status} to {to_status}.",
    "DOCUMENT_UPLOADED": "{doc_type} uploaded by customer.",
    "DOCUMENT_VERIFIED": "{doc_type} verified by the documentation team.",
    "DOCUMENT_REJECTED": "{doc_type} rejected: {reason}",
    "PAYMENT_INITIATED": "{purpose} of {amount} initiated via {method}.",
    "PAYMENT_SUCCESS": "{purpose} of {amount} received via {method}.",
    "PAYMENT_FAILED": "{purpose} of {amount} failed via {method}: {reason}",
    "PAYMENT_PENDING": "{purpose} of {amount} awaiting bank confirmation.",
    "REFUND_PROCESSED": "Refund of {amount} processed to the customer.",
    "FINANCE_APPLIED": "Loan application submitted to {partner}.",
    "FINANCE_UNDER_REVIEW": "Loan application under review with {partner}.",
    "FINANCE_APPROVED": "Loan of {amount} approved by {partner}.",
    "FINANCE_REJECTED": "Loan application rejected by {partner}.",
    "FINANCE_DISBURSED": "Loan of {amount} disbursed by {partner}.",
    "RC_TRANSFER_APPLIED": "RC transfer application filed with the RTO.",
    "RC_TRANSFER_COMPLETED": "RC transfer completed; ownership updated.",
    "RC_TRANSFER_REJECTED": "RC transfer rejected by the RTO.",
    "DELIVERY_SCHEDULED": "Delivery scheduled for {when} ({slot}).",
    "DELIVERY_ATTEMPT_FAILED": "Delivery attempt failed: {reason}",
    "DELIVERED": "Vehicle delivered to the customer.",
    "DELIVERY_CANCELLED": "Delivery cancelled.",
    "ORDER_CANCELLED": "Order cancelled: {reason}",
    "HOLD_APPLIED": "Order placed on hold: {reason}",
}
