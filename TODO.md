
todo:
- [x] add ui basis
    - shows your current pods and jobs
        - non controled pods that this app didnt start have a white circle, controled pods are green (actively recieving status messages), pods that are controled but connection was lost should be yellow, pods that have had an error are blinking red, pods that have unexpectadly disconnected are red
        - if a job doesnt have a runpod yet because the dataset is uploading, if a job is waiting for a pod it should blink blue, it should be blue, if a pod has been created and it hasnt been puppeteered yet it should blink blue,
    - clicking on a job/pod shows you information about the job/pod, its status and current log
    - settings ui for secrets
    - menu for creating builds
    - menu for starting a new job
        - select pod type, select build from ftp server
        - select dataset, if config file is detected use that, if not give option to select a config file
        - set if results should be downloaded automatically, or only put onto the ftp server
    - monitoring
        - create job (the job color will be green blinking)
        - upload dataset to ftp server as tar
        - start pod (if non of that type available wait)
        - upload scripts and start remote controling
- [x] for running lichtfield studio make the remote server act on its own without the clients supervision and shut itself down automatically after uploading the results to ftp, so that if the client gets disconnected the server still runs until the end.
- [ ] implement a build pipeline (later)
- [x] add option to terminate pod (Discard pod in the dashboard)
- [x] add an option to archive or delete a job (the listing not the data)
- [ ] create setup and start script for windows so users just have to execute a bat file
- [ ] create a setup and start script for linux users (debian/ubuntu)
- [ ] check app compatible with windows and linux (debian/ubuntu)
- [ ] i want to be able to create multiple jobs with the same dataset with diffferent config files. this should also be compatible with uploading. give the option to select a different jobs upload as dataset, and wait for it to finish before starting



## before releaser
- [x] make pod list refresh do every 30 seconds
- [x] make pressing enter in a field in the new job menu not create job as it could be pressed accidentally
- [x] add field for entering runpod image template name in the new job menu (or modifying it from default)
- [x] ftp uploads should end with .upload and then be renamed after the upload completes
- [x] when the remote server uploads the results to the ftp server it appends .upload to the folder and then renames the folder after all containing files are uploaded to the ftp server

- [x] add option to abort ftp upload

- [x] allow for a seperate config file for lichtfeld studio to be uploaded and then used by the pod instead of one included in the tar file
- [x] when uploading to ftp and the path selected is already a tar file, dont copy it, upload it from there (as option)

- [x] ftp upload show progress in ui
- [x] have a folder called datasets (if not create at startup), that is the default local folder for the app to look in for local datasets

- [x] make settings override in the new job menu for lichtfield studio settings optional in ui, so that only the config file gets used as settings for lichtfield studio, and nothing gets overriden

- [x] better file selection for local upload

- [x] add open result folder function for jobs that have suceeded and are locally downloaded

bugs
- after a job is done it switches to "completed presumably"
