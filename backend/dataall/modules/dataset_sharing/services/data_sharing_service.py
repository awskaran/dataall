import logging

from dataall.modules.dataset_sharing.services.share_processors.lf_process_cross_account_share import \
    ProcessLFCrossAccountShare
from dataall.modules.dataset_sharing.services.share_processors.lf_process_same_account_share import \
    ProcessLFSameAccountShare
from dataall.modules.dataset_sharing.services.share_processors.s3_access_point_process_share import \
    ProcessS3AccessPointShare
from dataall.modules.dataset_sharing.services.share_processors.s3_bucket_process_share import ProcessS3BucketShare

from dataall.base.db import Engine
from dataall.modules.dataset_sharing.db.enums import (ShareObjectActions, ShareItemStatus, ShareableType,
                                                      ShareItemActions)
from dataall.modules.dataset_sharing.db.share_object_repositories import ShareObjectSM, ShareObjectRepository, \
    ShareItemSM

log = logging.getLogger(__name__)


class DataSharingService:
    def __init__(self):
        pass

    @classmethod
    def approve_share(cls, engine: Engine, share_uri: str) -> bool:
        """
        1) Updates share object State Machine with the Action: Start
        2) Retrieves share data and items in Share_Approved state
        3) Calls sharing folders processor to grant share
        4) Calls sharing buckets processor to grant share
        5) Calls sharing tables processor for same or cross account sharing to grant share
        6) Updates share object State Machine with the Action: Finish

        Parameters
        ----------
        engine : db.engine
        share_uri : share uri

        Returns
        -------
        True if sharing succeeds,
        False if folder or table sharing failed
        """
        with engine.scoped_session() as session:
            (
                source_env_group,
                env_group,
                dataset,
                share,
                source_environment,
                target_environment,
            ) = ShareObjectRepository.get_share_data(session, share_uri)

            share_sm = ShareObjectSM(share.status)
            new_share_state = share_sm.run_transition(ShareObjectActions.Start.value)
            share_sm.update_state(session, share, new_share_state)

            (
                shared_tables,
                shared_folders,
                shared_buckets
            ) = ShareObjectRepository.get_share_data_items(session, share_uri, ShareItemStatus.Share_Approved.value)

        log.info(f'Granting permissions to folders: {shared_folders}')

        approved_folders_succeed = ProcessS3AccessPointShare.process_approved_shares(
            session,
            dataset,
            share,
            shared_folders,
            source_environment,
            target_environment,
            source_env_group,
            env_group
        )
        log.info(f'sharing folders succeeded = {approved_folders_succeed}')

        log.info('Granting permissions to S3 buckets')

        approved_s3_buckets_succeed = ProcessS3BucketShare.process_approved_shares(
            session,
            dataset,
            share,
            shared_buckets,
            source_environment,
            target_environment,
            source_env_group,
            env_group
        )
        log.info(f'sharing s3 buckets succeeded = {approved_s3_buckets_succeed}')

        if source_environment.AwsAccountId != target_environment.AwsAccountId:
            processor = ProcessLFCrossAccountShare(
                session,
                dataset,
                share,
                shared_tables,
                [],
                source_environment,
                target_environment,
                env_group,
            )
        else:
            processor = ProcessLFSameAccountShare(
                session,
                dataset,
                share,
                shared_tables,
                [],
                source_environment,
                target_environment,
                env_group
            )

        log.info(f'Granting permissions to tables: {shared_tables}')
        approved_tables_succeed = processor.process_approved_shares()
        log.info(f'sharing tables succeeded = {approved_tables_succeed}')

        new_share_state = share_sm.run_transition(ShareObjectActions.Finish.value)
        share_sm.update_state(session, share, new_share_state)

        return approved_folders_succeed and approved_s3_buckets_succeed and approved_tables_succeed

    @classmethod
    def revoke_share(cls, engine: Engine, share_uri: str):
        """
        1) Updates share object State Machine with the Action: Start
        2) Retrieves share data and items in Revoke_Approved state
        3) Calls sharing folders processor to revoke share
        4) Checks if remaining folders are shared and effectuates clean up with folders processor
        5) Calls sharing tables processor for same or cross account sharing to revoke share
        6) Checks if remaining tables are shared and effectuates clean up with tables processor
        7) Calls sharing buckets processor to revoke share
        8) Updates share object State Machine with the Action: Finish

        Parameters
        ----------
        engine : db.engine
        share_uri : share uri

        Returns
        -------
        True if revoke succeeds
        False if folder or table revoking failed
        """

        with engine.scoped_session() as session:
            (
                source_env_group,
                env_group,
                dataset,
                share,
                source_environment,
                target_environment,
            ) = ShareObjectRepository.get_share_data(session, share_uri)

            share_sm = ShareObjectSM(share.status)
            new_share_state = share_sm.run_transition(ShareObjectActions.Start.value)
            share_sm.update_state(session, share, new_share_state)

            revoked_item_sm = ShareItemSM(ShareItemStatus.Revoke_Approved.value)

            (
                revoked_tables,
                revoked_folders,
                revoked_buckets
            ) = ShareObjectRepository.get_share_data_items(session, share_uri, ShareItemStatus.Revoke_Approved.value)

            new_state = revoked_item_sm.run_transition(ShareObjectActions.Start.value)
            revoked_item_sm.update_state(session, share_uri, new_state)

            log.info(f'Revoking permissions to folders: {revoked_folders}')

            revoked_folders_succeed = ProcessS3AccessPointShare.process_revoked_shares(
                session,
                dataset,
                share,
                revoked_folders,
                source_environment,
                target_environment,
                source_env_group,
                env_group,
            )
            log.info(f'revoking folders succeeded = {revoked_folders_succeed}')
            existing_shared_folders = ShareObjectRepository.check_existing_shared_items_of_type(
                session,
                share_uri,
                ShareableType.StorageLocation.value
            )
            existing_shared_buckets = ShareObjectRepository.check_existing_shared_items_of_type(
                session,
                share_uri,
                ShareableType.S3Bucket.value
            )
            existing_shared_items = existing_shared_folders or existing_shared_buckets
            log.info(f'Still remaining S3 resources shared = {existing_shared_items}')
            if not existing_shared_folders and revoked_folders:
                log.info("Clean up S3 access points...")
                clean_up_folders = ProcessS3AccessPointShare.clean_up_share(
                    session,
                    dataset=dataset,
                    share=share,
                    folder=revoked_folders[0],
                    source_environment=source_environment,
                    target_environment=target_environment,
                    source_env_group=source_env_group,
                    env_group=env_group
                )
                log.info(f"Clean up S3 successful = {clean_up_folders}")

            log.info('Revoking permissions to S3 buckets')

            revoked_s3_buckets_succeed = ProcessS3BucketShare.process_revoked_shares(
                session,
                dataset,
                share,
                revoked_buckets,
                source_environment,
                target_environment,
                source_env_group,
                env_group
            )
            log.info(f'revoking s3 buckets succeeded = {revoked_s3_buckets_succeed}')

            if source_environment.AwsAccountId != target_environment.AwsAccountId:
                processor = ProcessLFCrossAccountShare(
                    session,
                    dataset,
                    share,
                    [],
                    revoked_tables,
                    source_environment,
                    target_environment,
                    env_group,
                )
            else:
                processor = ProcessLFSameAccountShare(
                    session,
                    dataset,
                    share,
                    [],
                    revoked_tables,
                    source_environment,
                    target_environment,
                    env_group)

            log.info(f'Revoking permissions to tables: {revoked_tables}')
            revoked_tables_succeed = processor.process_revoked_shares()
            log.info(f'revoking tables succeeded = {revoked_tables_succeed}')

            existing_shared_items = ShareObjectRepository.check_existing_shared_items_of_type(
                session,
                share_uri,
                ShareableType.Table.value
            )
            log.info(f'Still remaining LF resources shared = {existing_shared_items}')
            if not existing_shared_items and revoked_tables:
                log.info("Clean up LF remaining resources...")
                clean_up_tables = processor.delete_shared_database()
                log.info(f"Clean up LF successful = {clean_up_tables}")

            existing_pending_items = ShareObjectRepository.check_pending_share_items(session, share_uri)
            if existing_pending_items:
                new_share_state = share_sm.run_transition(ShareObjectActions.FinishPending.value)
            else:
                new_share_state = share_sm.run_transition(ShareObjectActions.Finish.value)
            share_sm.update_state(session, share, new_share_state)

            return revoked_folders_succeed and revoked_s3_buckets_succeed and revoked_tables_succeed
