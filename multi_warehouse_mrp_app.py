import streamlit as st
import pandas as pd
import numpy as np
from datetime import timedelta, date
import io

st.set_page_config(layout="wide")
st.title("Material Requirement Planning Automation")

# -------------------------------
# Helper: Clean Columns
# -------------------------------
def clean_columns(df):
    df.columns = df.columns.str.strip().str.lower().str.replace(" ", "_")
    return df

def clean_numeric(df, columns):
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    return df

# -------------------------------
# Upload Input Files
# -------------------------------
st.header("Upload Input Files")

col1, col2, col3, col4 = st.columns(4)
with col1:
    inventory_file = st.file_uploader("Inventory On Hand", type=["csv", "xlsx"])
with col2:
    issuance_file = st.file_uploader("Historical Weekly Issuance", type=["csv", "xlsx"])
with col3:
    receipts_file = st.file_uploader("Scheduled Receipts", type=["csv", "xlsx"])
with col4:
    item_master_file = st.file_uploader("Item Master", type=["csv", "xlsx"])

if inventory_file and issuance_file and receipts_file and item_master_file:

    # -------------------------------
    # Load Files
    # -------------------------------
    def load_file(f):
        if f.name.lower().endswith(".xlsx"):
            return pd.read_excel(f)
        else:
            try:
                return pd.read_csv(f, encoding="utf-8")
            except:
                return pd.read_csv(f, encoding="latin1")

    inventory = clean_columns(load_file(inventory_file))
    issuance = clean_columns(load_file(issuance_file))
    receipts = clean_columns(load_file(receipts_file))
    items = clean_columns(load_file(item_master_file))

    # -------------------------------
    # Clean numeric columns
    # -------------------------------
    inventory = clean_numeric(inventory, ["on_hand_qty"])
    issuance = clean_numeric(issuance, ["issued_qty"])
    receipts = clean_numeric(receipts, ["qty"])
    items = clean_numeric(items, ["safety_stock", "lead_time", "moq", "pack_size"])

    # Ensure UOM is string
    for df in [inventory, issuance, receipts, items]:
        if "uom" in df.columns:
            df["uom"] = df["uom"].astype(str)

    st.success("Files loaded successfully!")

    # -------------------------------
    # Validate Item Master
    # -------------------------------
    required_cols = ["warehouse","item_id","description","safety_stock","lead_time","moq","pack_size","uom"]
    missing = [c for c in required_cols if c not in items.columns]
    if missing:
        st.error(f"Missing required columns in Item Master: {missing}")
        st.stop()

    dupes = items[items.duplicated(subset=["warehouse","item_id"], keep=False)]
    if not dupes.empty:
        st.error("Duplicate warehouse-item combinations found.")
        st.dataframe(dupes)
        st.stop()

    items_dict = items.set_index(["warehouse","item_id"]).to_dict("index")

    # -------------------------------
    # Date Conversion
    # -------------------------------
    for df in [issuance, receipts]:
        if "week_start" in df.columns:
            df["week_start"] = pd.to_datetime(df["week_start"], errors='coerce')

    # -------------------------------
    # Planning Horizon
    # -------------------------------
    today = pd.to_datetime(date.today())
    today_monday = today - pd.Timedelta(days=today.weekday())
    num_weeks = 12
    time_buckets = [today_monday + timedelta(weeks=w) for w in range(num_weeks)]

    # -------------------------------
    # Pre-group Data for Performance
    # -------------------------------
    issuance_grouped = issuance.groupby(["warehouse","item_id"])["issued_qty"].apply(list).to_dict()
    receipts_grouped = receipts.groupby(["warehouse","item_id","week_start"])["qty"].sum().to_dict()
    inventory_grouped = inventory.groupby(["warehouse","item_id"])["on_hand_qty"].sum().to_dict()

    # -------------------------------
    # MRP Logic
    # -------------------------------
    debug_rows = []

    for (wh, item), params in items_dict.items():
        previous_s = float(inventory_grouped.get((wh,item), 0))
        safety_stock = float(params.get("safety_stock", 0))
        lead_time = int(params.get("lead_time", 1))
        moq = int(params.get("moq", 1))
        pack_size = int(params.get("pack_size", 1))
        description = params.get("description", "")
        uom = params.get("uom", "")

        issued_list = issuance_grouped.get((wh, item), [])
        avg_demand = np.mean(issued_list[-4:]) if len(issued_list) > 0 else 1
        avg_demand = max(float(avg_demand), 1)  # ensures weekly requirement never zero

        for bucket in time_buckets:
            incoming_qty = float(receipts_grouped.get((wh,item,bucket), 0))
            wkly_req = avg_demand

            end_s = previous_s - wkly_req + incoming_qty
            shortage = max(safety_stock - end_s, 0)
            planned_qty = 0

            if shortage > 0:
                planned_qty = max(shortage, moq)
                if pack_size > 0:
                    planned_qty = np.ceil(planned_qty / pack_size) * pack_size
                end_s += planned_qty

            debug_rows.append({
                "warehouse": wh,
                "item": item,
                "description": description,
                "uom": uom,
                "week": bucket,
                "Beg_SOH": previous_s,
                "Wkly_Req": wkly_req,
                "Incoming": incoming_qty,
                "Planned_Order": planned_qty,
                "End_SOH": end_s
            })

            previous_s = end_s

    debug_df = pd.DataFrame(debug_rows)

    # -------------------------------
    # Display Parameter Table
    # -------------------------------
    st.subheader("MRP Logic Table")
    st.dataframe(debug_df)

    # -------------------------------
    # Planned Orders Table
    # -------------------------------
    planned_df = debug_df[debug_df["Planned_Order"] > 0].copy()
    if not planned_df.empty:
        planned_df.rename(columns={"Planned_Order":"planned_qty"}, inplace=True)
        st.subheader("Upcoming Planned Orders")
        st.dataframe(planned_df)

        # Excel download
        output = io.BytesIO()
        planned_df.to_excel(output, index=False, engine='openpyxl')
        output.seek(0)
        st.download_button(
            "📥 Download Planned Orders as Excel",
            data=output,
            file_name="planned_orders.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.warning("No upcoming planned orders.")
