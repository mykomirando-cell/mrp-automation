import streamlit as st
import pandas as pd
import numpy as np
from datetime import timedelta, date
import io

st.set_page_config(layout="wide")
st.title("Material Requirement Planning Automation")

# -------------------------------
# ROBUST FILE LOADER (Cloud Safe)
# -------------------------------
def load_file(f):

    if f.name.lower().endswith(".xlsx"):
        return pd.read_excel(f)

    elif f.name.lower().endswith(".csv"):
        try:
            return pd.read_csv(f, encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return pd.read_csv(f, encoding="utf-8-sig")
            except:
                return pd.read_csv(f, encoding="latin1")
    else:
        st.error(f"Unsupported file type: {f.name}")
        st.stop()

# -------------------------------
# 1️⃣ Upload Input Files
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

# -------------------------------
# EXECUTION GUARD
# -------------------------------
if not all([inventory_file, issuance_file, receipts_file, item_master_file]):
    st.warning("Waiting for all required uploads...")
    st.stop()

# -------------------------------
# LOAD FILES
# -------------------------------
inventory = load_file(inventory_file)
issuance = load_file(issuance_file)
receipts = load_file(receipts_file)
items = load_file(item_master_file)

for df in [inventory, issuance, receipts, items]:
    df["uom"] = df["uom"].astype(str)

issuance["week_start"] = pd.to_datetime(issuance["week_start"])
receipts["week_start"] = pd.to_datetime(receipts["week_start"])

st.success("Files loaded successfully!")

# -------------------------------
# 2️⃣ SAFE UOM Consistency Check
# -------------------------------
uom_errors = []

for item in items["item_id"].dropna().unique():

    uoms = set()

    base = items.loc[items["item_id"]==item, "uom"]
    if not base.empty:
        uoms.add(base.iloc[0])

    inv = inventory.loc[inventory["item_id"]==item, "uom"]
    if not inv.empty:
        uoms.update(inv.unique())

    iss = issuance.loc[issuance["item_id"]==item, "uom"]
    if not iss.empty:
        uoms.update(iss.unique())

    rec = receipts.loc[receipts["item_id"]==item, "uom"]
    if not rec.empty:
        uoms.update(rec.unique())

    if len(uoms) > 1:
        uom_errors.append((item, list(uoms)))

if uom_errors:
    st.error("UOM mismatch detected")
    st.dataframe(pd.DataFrame(uom_errors, columns=["Item","Detected UOMs"]))
    st.stop()

# -------------------------------
# 3️⃣ Item Master Validation
# -------------------------------
required_cols = ["warehouse","item_id","description","safety_stock","lead_time","MOQ","pack_size","uom"]
missing = [c for c in required_cols if c not in items.columns]

if missing:
    st.error(f"Missing required columns in Item Master: {missing}")
    st.stop()

dupes = items[items.duplicated(subset=["warehouse","item_id"], keep=False)]
if not dupes.empty:
    st.error("Duplicate warehouse-item planning parameters:")
    st.dataframe(dupes)
    st.stop()

items_dict = items.set_index(["warehouse","item_id"]).to_dict("index")
warehouses = inventory["warehouse"].unique()

# -------------------------------
# 4️⃣ Planning Weeks (Always Monday)
# -------------------------------
today = pd.to_datetime(date.today())
year_start = pd.to_datetime(f"{today.year}-01-01")

first_monday = year_start + pd.Timedelta(days=(7-year_start.weekday())%7)
last_monday = pd.to_datetime(f"{today.year}-12-31") - pd.Timedelta(days=pd.to_datetime(f"{today.year}-12-31").weekday())

time_buckets = pd.date_range(start=first_monday,end=last_monday,freq="W-MON")

# -------------------------------
# 5️⃣ MRP ENGINE
# -------------------------------
debug_rows = []

for wh in warehouses:

    wh_inventory = inventory[inventory["warehouse"]==wh].set_index("item_id")["on_hand_qty"].to_dict()
    wh_items = [item for (w,item) in items_dict.keys() if w==wh]

    for item in wh_items:

        previous_s = wh_inventory.get(item,0)
        params = items_dict.get((wh,item),{})

        safety_stock = params.get("safety_stock",0)
        lead_time = int(params.get("lead_time",1))
        MOQ = int(params.get("MOQ",1))
        pack_size = int(params.get("pack_size",1))
        uom = params.get("uom","")
        description = params.get("description","")

        last4 = issuance[(issuance["warehouse"]==wh)&(issuance["item_id"]==item)].sort_values("week_start").tail(4)
        avg_demand = last4["issued_qty"].mean() if not last4.empty else 0
        avg_demand = max(avg_demand,1)

        for bucket in time_buckets:

            incoming = receipts[(receipts["warehouse"]==wh)&(receipts["item_id"]==item)&(receipts["week_start"]==bucket)]
            incoming_qty = incoming["qty"].sum() if not incoming.empty else 0

            end_s = previous_s - avg_demand + incoming_qty
            shortage = max(safety_stock - end_s,0)

            planned_qty = 0
            if shortage>0:
                planned_qty = max(shortage,MOQ)
                planned_qty = int(np.ceil(planned_qty/pack_size)*pack_size)
                end_s += planned_qty

            debug_rows.append({
                "warehouse":wh,
                "item":item,
                "description":description,
                "uom":uom,
                "week":bucket,
                "Beg_SOH":previous_s,
                "Wkly_Req":avg_demand,
                "Incoming":incoming_qty,
                "Shortage":shortage,
                "Planned_Order":planned_qty,
                "End_SOH":end_s,
                "Safety_Stock":safety_stock
            })

            previous_s = end_s

debug_df = pd.DataFrame(debug_rows)

# -------------------------------
# 6️⃣ Planned Orders
# -------------------------------
planned_rows = []

for _,r in debug_df.iterrows():
    if r["Planned_Order"]>0:
        params = items_dict.get((r["warehouse"],r["item"]),{})
        lt = int(params.get("lead_time",1))
        release = r["week"] - pd.Timedelta(weeks=lt)

        planned_rows.append({
            "warehouse":r["warehouse"],
            "item":r["item"],
            "description":params.get("description",""),
            "uom":r["uom"],
            "planned_qty":r["Planned_Order"],
            "receipt_week":r["week"],
            "release_week":release
        })

planned_df = pd.DataFrame(planned_rows)

st.subheader("Upcoming Planned Orders")

if planned_df.empty:
    st.warning("No upcoming planned orders.")
else:
    planned_df.sort_values(["release_week","warehouse","item"], inplace=True)
    st.dataframe(planned_df)

    output = io.BytesIO()
    planned_df.to_excel(output,index=False,engine="openpyxl")
    output.seek(0)

    st.download_button(
        "Download Planned Orders",
        output,
        "planned_orders.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
