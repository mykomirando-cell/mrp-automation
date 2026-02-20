import streamlit as st
import pandas as pd
import numpy as np

st.set_page_config(page_title="Multi-Warehouse MRP", layout="wide")

st.title("📦 Multi-Warehouse MRP Planning Tool")

# -------------------------------
# File Upload Section
# -------------------------------

st.sidebar.header("Upload Required Files")

item_file = st.sidebar.file_uploader("Upload Item Master", type=["xlsx", "csv"])
demand_file = st.sidebar.file_uploader("Upload Demand Forecast", type=["xlsx", "csv"])
inventory_file = st.sidebar.file_uploader("Upload Inventory On Hand", type=["xlsx", "csv"])

# -------------------------------
# Helper Functions
# -------------------------------

def load_file(file):
    if file.name.endswith(".csv"):
        return pd.read_csv(file)
    else:
        return pd.read_excel(file)

def clean_columns(df):
    df.columns = df.columns.str.strip()
    return df

def clean_numeric(df, columns):
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df

# -------------------------------
# Main Logic
# -------------------------------

if item_file and demand_file and inventory_file:

    # Load files
    item_master = clean_columns(load_file(item_file))
    demand = clean_columns(load_file(demand_file))
    inventory = clean_columns(load_file(inventory_file))

    # Required columns
    required_item_cols = [
        "warehouse",
        "item_id",
        "description",
        "uom",
        "lead_time",
        "safety_stock",
        "MOQ",
        "pack_size"
    ]

    missing_cols = [col for col in required_item_cols if col not in item_master.columns]

    if missing_cols:
        st.error(f"Missing required columns in Item Master: {missing_cols}")
        st.stop()

    # Clean numeric columns safely
    numeric_cols = ["lead_time", "safety_stock", "MOQ", "pack_size"]
    item_master = clean_numeric(item_master, numeric_cols)

    # Clean demand + inventory
    demand = clean_numeric(demand, ["forecast_qty"])
    inventory = clean_numeric(inventory, ["on_hand_qty"])

    # -------------------------------
    # Merge Data
    # -------------------------------

    param_table = (
        item_master
        .merge(demand, on=["warehouse", "item_id"], how="left")
        .merge(inventory, on=["warehouse", "item_id"], how="left")
    )

    param_table["forecast_qty"] = param_table["forecast_qty"].fillna(0)
    param_table["on_hand_qty"] = param_table["on_hand_qty"].fillna(0)

    # -------------------------------
    # MRP Logic
    # -------------------------------

    param_table["net_requirement"] = (
        param_table["forecast_qty"]
        + param_table["safety_stock"]
        - param_table["on_hand_qty"]
    )

    param_table["net_requirement"] = param_table["net_requirement"].apply(
        lambda x: max(x, 0)
    )

    # Apply MOQ
    param_table["planned_order_qty"] = np.where(
        param_table["net_requirement"] > 0,
        np.maximum(param_table["net_requirement"], param_table["MOQ"]),
        0
    )

    # Apply pack size rounding
    param_table["planned_order_qty"] = np.where(
        param_table["planned_order_qty"] > 0,
        np.ceil(param_table["planned_order_qty"] / param_table["pack_size"]) * param_table["pack_size"],
        0
    )

    # -------------------------------
    # Display Results (SAFE VERSION)
    # -------------------------------

    st.subheader("MRP Logic Table")
    st.dataframe(param_table)

    # -------------------------------
    # Planned Orders Summary
    # -------------------------------

    planned_orders = param_table[param_table["planned_order_qty"] > 0][
        [
            "warehouse",
            "item_id",
            "description",
            "planned_order_qty",
            "lead_time"
        ]
    ]

    st.subheader("Planned Orders")
    st.dataframe(planned_orders)

else:
    st.info("Please upload all three required files to run MRP.")
