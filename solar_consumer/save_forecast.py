from loguru import logger
from nowcasting_datamodel.save.save import save
from pvsite_datamodel.write.generation import insert_generation_values
from pvsite_datamodel.write.forecast import insert_forecast_values
from pvsite_datamodel.read.site import get_site_by_client_site_name
from pvsite_datamodel.write.user_and_site import create_site
from pvsite_datamodel.pydantic_models import PVSiteEditMetadata as PVSite
from sqlalchemy.orm.session import Session
import os
import pandas as pd
from typing import Optional

# Default NL national site
nl_national = PVSite(client_site_name="nl_national", latitude="52.15", longitude="5.23")

# Germany Transmission System Operators (TSOs)
# Coords ~direct to HQs
de_50hertz = PVSite(client_site_name="50Hertz", latitude="52.53", longitude="13.37")
de_amprion = PVSite(client_site_name="Amprion", latitude="51.52", longitude="7.45")
de_tennet = PVSite(client_site_name="TenneT", latitude="52.38", longitude="5.17")
de_transnetbw = PVSite(client_site_name="TransnetBW", latitude="48.78", longitude="9.18")
DE_TSO_SITES = {"TransnetBW": de_transnetbw, "50Hertz": de_50hertz, "TenneT": de_tennet,
                "Amprion": de_amprion}
# Actual installed capacities (in kW) by TSO
# 50Hz via 2022 report, all others via 2020 OSPD dataset
DE_TSO_CAPACITY = {"TransnetBW": 10_770_000, "50Hertz": 18_175_000, "TenneT": 21_882_000, 
                   "Amprion": 16_506_000}


# Get or create a PVSite in the database
def get_or_create_pvsite(session: Session, pvsite: PVSite, country: str, 
                         capacity_override_kw: Optional[int] = None,):
    
    try:
        site = get_site_by_client_site_name(
            session=session,
            client_site_name=pvsite.client_site_name,
            client_name=pvsite.client_site_name, # this is not used
        )
    except Exception:
        logger.info(f"Creating site {pvsite.client_site_name} in the database.")
        
        # Choose capacity based on country; per-TSO for de; nl only has 20GW hard‑coded
        if capacity_override_kw is not None:
            capacity = capacity_override_kw
        elif country == "de":
            capacity = DE_TSO_CAPACITY[pvsite.client_site_name]
        else:
            capacity = 20_000_000
        
        site, _ = create_site(
            session=session,
            latitude=pvsite.latitude,
            longitude=pvsite.longitude,
            client_site_name=pvsite.client_site_name,
            client_site_id=1,
            country=country,
            capacity_kw=capacity,
            dno="", # these are UK specific things
            gsp="", # these are UK specific things
        )
    return site

def save_generation_to_site_db(
    generation_data: pd.DataFrame, session: Session, country: str = "nl"
):
    """Save generation data to the database.

    Parameters:
        generation_data (pd.DataFrame): DataFrame containing generation data to save.
            The following columns must be present: 
            - solar_generation_kw
            - target_datetime_utc
            - capacity_kw (only when country="nl")
            - tso_zone (only when country="de")
        session (Session): SQLAlchemy session for database access.
        country: (str): Country code for the generation data ('nl' or 'de')
    
    Return:
        None
    """
    # Check if generation_data is empty
    if generation_data.empty:
        logger.warning("No generation data provided to save!")
        return

    # Determine country
    if country == "nl":
        country_sites = {"nl_national": nl_national}
    elif country == "de":
        country_sites = DE_TSO_SITES
    else:
        raise Exception("Only generation data from the following countries is supported \
            when saving: 'nl', 'de'")

    # Derive capacity override once (test expects max row value if present)
    capacity_override = (
        int(generation_data["capacity_kw"].max())
        if "capacity_kw" in generation_data.columns
        else None
    )

    # Loop per site
    for tso, pvsite in country_sites.items():
        
        # Filter by TSO for Germany, or use all data for NL
        if country == "de":
            generation_data_tso_df = generation_data[generation_data["tso_zone"] == tso].copy()
        else:
            generation_data_tso_df = generation_data.copy()
            
        if generation_data_tso_df.empty:
            logger.debug(f"No rows for TSO {tso!r}, skipping")
            continue
            
        # Create or fetch site and pass same override for any country
        site = get_or_create_pvsite(session, pvsite, country, 
                                    capacity_override_kw=capacity_override,)

        # Prepare DataFrame, rename and insert
        generation_data_tso_df = generation_data_tso_df.rename(
            columns={
                "solar_generation_kw": "power_kw",
                "target_datetime_utc": "start_utc",
            }
        )
        generation_data_tso_df["start_utc"] = pd.to_datetime(generation_data_tso_df["start_utc"])
        generation_data_tso_df["site_uuid"] = site.location_uuid

        insert_generation_values(session=session, df=generation_data_tso_df)
        session.commit()
        logger.info(f"Successfully saved {len(generation_data_tso_df)} rows")

def save_forecasts_to_site_db(
    forecast_data: pd.DataFrame,
    session: Session,
    model_tag: str,
    model_version: str,
    country: str = "nl",
):
    """Save generation data to the database.

    Parameters:
        forecast_data (pd.DataFrame): DataFrame containing generation data to save.
            The following columns must be present: solar_generation_kw and target_datetime_utc
        session (Session): SQLAlchemy session for database access.
        model_tag (str): Model tag to fetch model metadata.
        model_version (str): Model version to fetch model metadata.
        country: (str): Country code for the generation data. Currently only 'nl' is supported.
        
    Return:
        None
    """

    if country != "nl":
        raise Exception("Only NL forecast data is supported when saving (atm).")

    site = get_or_create_pvsite(session, nl_national, country)

    timestamp_utc = pd.Timestamp.now(tz="UTC").floor("15min")

    forecast_meta = {
        "location_uuid": site.location_uuid,
        "timestamp_utc": timestamp_utc,
        "forecast_version": model_version,
    }

    forecast_data.rename(
        columns={
            "solar_generation_kw": "forecast_power_kw",
            "target_datetime_utc": "start_utc",
        },
        inplace=True,
    )

    # drop other rows and add end_utc
    forecast_data = forecast_data[["forecast_power_kw", "start_utc"]]
    forecast_data["end_utc"] = forecast_data["start_utc"] + pd.Timedelta(hours=0.25)

    # calculate horizon minutes
    forecast_data["horizon_minutes"] = (
        forecast_data["start_utc"] - timestamp_utc
    ).dt.total_seconds() / 60

    insert_forecast_values(
        forecast_values_df=forecast_data,
        forecast_meta=forecast_meta,
        ml_model_name=model_tag,
        ml_model_version=model_version,
        session=session,
    )


def save_forecasts_to_db(forecasts: list, session: Session):
    """Save forecasts to the database.

    Parameters:
        forecasts (list): List of forecast objects to save.
        session (Session): SQLAlchemy session for database access.

    Return:
        None
    """
    # Check if forecasts is empty
    if not forecasts:
        logger.warning("No forecasts provided to save!")
        return

    try:
        logger.info("Saving forecasts to the database.")
        save(
            forecasts=forecasts,
            session=session,
        )
        logger.info(f"Successfully saved {len(forecasts)} forecasts to the database.")
    except Exception as e:
        logger.error(f"An error occurred while saving forecasts: {e}")
        raise e


def save_forecasts_to_csv(forecasts: pd.DataFrame, csv_dir: str):
    """Save forecasts to a CSV file.

    Parameters:
        forecasts (pd.DataFrame): DataFrame containing forecast data to save.
        csv_dir (str): Directory to save CSV files.

    Return:
        None
    """
    # Check if forecasts is empty
    if forecasts.empty:
        logger.warning("No forecasts provided to save!")
        return

    if not csv_dir:  # check if directory csv directory provided
        raise ValueError("CSV directory is not provided for CSV saving.")

    os.makedirs(csv_dir, exist_ok=True)
    csv_path = os.path.join(csv_dir, "forecast_data.csv")

    try:
        forecasts.drop(
            columns=["_sa_instance_state"], errors="ignore", inplace=True
        )  # Remove SQLAlchemy metadata

        logger.info(f"Saving forecasts to CSV at {csv_path}")
        forecasts.to_csv(csv_path, index=False)
        logger.info(f"Successfully saved {len(forecasts)} forecasts to CSV.")
    except Exception as e:
        logger.error(f"An error occurred while saving forecasts to CSV: {e}")
        raise e
